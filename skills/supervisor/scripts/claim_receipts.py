# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read maintained claim/check receipts from native tool outputs, never chat prose."""
from __future__ import annotations

import json
import re
import shlex
import ast
import argparse
import io
import textwrap
import tokenize
import shutil
import subprocess
import importlib.util
import sys
from functools import lru_cache
from contextlib import redirect_stdout
from datetime import datetime
import posixpath
from pathlib import Path

CHECK_OPERATION = "work-closeout.claims"
CLAIM_OPERATION = "github.plan.claim"
HIDDEN_UV_RUN_OPTIONS = {"--python-preference": True, "--native-tls": False}
COMMENT = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/([1-9]\d*)#issuecomment-([1-9]\d*)")
SHELL_FIELD = re.compile(r'''\btools\.exec_command\s*\(\s*\{(?:(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)|[^{}"'`])*?(?:\b(?:cmd|command)|"(?:cmd|command)"|'(?:cmd|command)')\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')(?=\s*[,}])''')
OWNERSHIP_TEXT = re.compile(r"gh-plan\.py[\s\S]*\bclaim\b|gh-pr\.py[\s\S]*\bcreate\b|\bgh\s+pr\s+create\b")


class ReceiptCoverageError(ValueError):
    pass


class ExecutableOwnership(ValueError):
    """A parsed embedded program executes ownership, rather than unknown syntax."""


def envelopes(value, depth=0):
    """Unwrap native text blocks and exec results, stopping at helper envelopes.

    Do not interpret issue bodies, quoted receipts or assistant text as writes.
    Both native harnesses and Codex's composed exec output use these wrappers.
    """
    if depth > 16:
        raise ReceiptCoverageError("tool output nesting exceeds receipt coverage")
    if isinstance(value, dict):
        if "operation" in value:
            yield value
            return
        if isinstance(value.get("session_id"), int) or isinstance(value.get("exit_code"), int):
            yield value  # native running or terminal exec result
        for key in ("output", "text", "content", "value", "result"):
            if key in value:
                yield from envelopes(value[key], depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from envelopes(child, depth + 1)
    elif isinstance(value, str):
        decoder, cursor = json.JSONDecoder(), 0
        while (start := value.find("{", cursor)) >= 0:
            try:
                item, cursor = decoder.raw_decode(value, start)
            except ValueError:
                cursor = start + 1
                continue
            yield from envelopes(item, depth + 1)


def raw_outputs(records, harness, *, call_id=None):
    for index, record in enumerate(records):
        if harness == "codex":
            payload = record.get("payload") or {}
            if (payload.get("type") in {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}
                    and (call_id is None or payload.get("call_id") == call_id)):
                for value in envelopes(payload.get("output")):
                    yield index, {**value, "_tool_call_id": payload.get("call_id")}
        else:
            message = record.get("message")
            for block in (message.get("content") or []) if isinstance(message, dict) else []:
                if (isinstance(block, dict) and block.get("type") == "tool_result"
                        and (call_id is None or block.get("tool_use_id") == call_id)):
                    for value in envelopes(block.get("content")):
                        yield index, {**value, "_tool_call_id": block.get("tool_use_id"), "_tool_error": block.get("is_error") is True}


def outputs(records, harness):
    """Decode receipts only after binding each native result to its command.

    Reads may contain arbitrary saved reports and deeply nested data. They are
    neither new ownership nor checker evidence, including inside serial cells.
    """
    positions = list(range(len(records)))
    if harness == "codex":
        records, positions = serial_ownership_records(records)
    def relevant(name, value):
        return (opaque_ownership(name, value)
                or any(ownership_command(argv) or script(argv) == "check_claims.py"
                       for argv in shell_commands(name, value)))
    isolated = {identifier: helper for record in records
                for identifier, name, value in calls_and_results(record, harness)[0]
                for helper in ("gh-plan.py", "gh-pr.py") if isolated_command(name, value, helper)}
    for index, source, value in bound_outputs(records, harness, relevant, isolated_polls=True,
                                         opaque_calls=file_ownership_executions(records, harness)):
        operation = value.get("operation")
        if (operation == CLAIM_OPERATION and isolated.get(source) != "gh-plan.py"
                or operation == "github.pr.create" and isolated.get(source) != "gh-pr.py"):
            continue
        if "_attempt" not in value and "_result" not in value:
            yield positions[index], value


def calls_and_results(record, harness):
    payload = record.get("payload") or {}
    if harness == "codex":
        calls = [(payload.get("call_id"), payload.get("name"), payload.get("arguments") or payload.get("input"))] if payload.get("type") in {"function_call", "custom_tool_call"} else []
        if payload.get("type") == "local_shell_call":
            calls = [(payload.get("call_id") or payload.get("id"), "local_shell_call", payload.get("action"))]
        results = [payload.get("call_id")] if payload.get("type") in {"function_call_output", "custom_tool_call_output", "local_shell_call_output"} else []
    else:
        message = record.get("message")
        blocks = (message.get("content") or []) if isinstance(message, dict) else []
        calls = [(b.get("id"), b.get("name"), b.get("input")) for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        results = [b.get("tool_use_id") for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
    return calls, results


def arguments(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def shell_texts(name, value):
    value = arguments(value)
    if name in {"exec_command", "Bash", "shell", "shell_command", "local_shell_call"} and isinstance(value, dict):
        texts = [value.get("cmd") or value.get("command") or ""]
        if isinstance(texts[0], list):
            argv = texts[0]
            body = shell_c_body(argv)
            texts = [body] if body is not None else [shlex.join(argv)]
    elif name in {"exec", "functions.exec"} and isinstance(value, str):
        texts = []
        for m in SHELL_FIELD.finditer(value):
            try:
                texts.append(ast.literal_eval(m[1]))
            except (ValueError, SyntaxError):
                pass
    else:
        return
    yield from texts


def shell_commands(name, value, *, depth=0):
    """Only shell tools execute commands; tool prompts and echo operands do not."""
    def commands(argv):
        child = unwrapped_command(argv)
        body = shell_c_body(child)
        if depth < 16 and body is not None and foreground_shell_text(body):
            yield from shell_commands("exec_command", {"cmd": body}, depth=depth + 1)
        else:
            yield argv[1:] if argv[0] == "nohup" else argv
    texts = shell_texts(name, value)
    for text in texts:
        try:
            lexer = shell_lexer(shell_code(text))
            lexer.whitespace = " \t\r"
            lexer.whitespace_split = True
            command = []
            for token in lexer:
                if token and all(c in ";&|\n" for c in token):
                    if command:
                        yield from commands(command)
                    command = []
                else:
                    command.append(token)
            if command:
                yield from commands(command)
        except ValueError:
            continue


def xargs_command(argv):
    """Read xargs' child after its options; operands are never executable words.

    None preserves unresolved option arity. An empty child is default echo, an
    informational invocation, or input redirected away from the native stdin.
    """
    words, cursor, input_file = list(argv[1:]), 0, None
    long_values = {"--max-args", "--max-lines", "--max-procs", "--delimiter", "--max-chars", "--arg-file", "--process-slot-var"}
    short_values = {"I", "n", "L", "P", "d", "E", "s", "a"}
    while cursor < len(words):
        word = words[cursor]
        if word == "--":
            cursor += 1
            break
        if word == "-" or not word.startswith("-"):
            break
        option, equal, inline = word.partition("=")
        if option in {"--help", "--version"}:
            return []
        if word.startswith("--"):
            cursor += 1
            if option in long_values:
                if not equal:
                    if cursor == len(words):
                        return []
                    inline, cursor = words[cursor], cursor + 1
                if option == "--arg-file":
                    input_file = inline
            elif option not in {"--replace", "--eof", "--null", "--no-run-if-empty", "--verbose", "--interactive", "--exit", "--open-tty", "--show-limits"}:
                return None
            continue
        cursor += 1
        for index, flag in enumerate(word[1:], 1):
            if flag in short_values:
                operand = word[index + 1:]
                if not operand:
                    if cursor == len(words):
                        return []
                    operand, cursor = words[cursor], cursor + 1
                if flag == "a":
                    input_file = operand
                break
            if flag in {"e", "i", "l"}:
                break  # Historical optional short operands are attached only.
            if flag not in {"0", "p", "r", "t", "x", "o"}:
                return None
    if input_file is not None and input_file not in {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"} and not input_file.startswith("$"):
        return []
    return words[cursor:]


def argument_execution_language(argv):
    """Classify actual executable argv, keeping interpreter code operands intact."""
    child = unwrapped_command(argv, unwrap_code=False)
    if not child:
        return None
    name = Path(child[0]).name
    if child[0].startswith("$") or name in {"bash", "sh", "zsh", "dash", "ksh", "node", "source"} or child[0] == "." or re.fullmatch(r"python[0-9.]*", name):
        return "argv"
    if name == "xargs":
        executable = xargs_command(child)
        return "argv" if executable is None else argument_execution_language(executable)
    helper, action = helper_action(child)
    if helper in {"gh-plan.py", "gh-pr.py"}:
        return "argv" if not action or action[:1] in (["claim"], ["create"]) or any(c in action[0] for c in "$`") else None
    if helper == "gh":
        return "argv" if not action or action[:2] == ["pr", "create"] or action == ["pr"] else None
    if name in {"su", "runuser", "sudo", "doas", "ssh", "flock"}:
        children = list(executor_commands(child))
        return "argv" if any(argument_execution_language(shlex.split(command)) for command in children) else None
    return "argv" if executable_wrapper(child) else None


def stdin_language(argv):
    """Identify interpreters that read code from stdin, rather than a script argv."""
    argv = unwrapped_command(argv)
    if not argv:
        return None
    name, words = Path(argv[0]).name, argv[1:]
    if name == "uv":
        return "argv" if words[:1] == ["run"] or words and words[0].startswith("-") and "run" in words else None
    if name in {"sudo", "doas", "flock", "ssh", "su", "runuser"}:
        if name in {"su", "runuser"} and any(word in {"-h", "--help", "-V", "--version"} for word in words):
            return None
        children = list(executor_commands(argv))
        languages = {shell_stdin_language(command) for command in children}
        languages.discard(None)
        if languages:
            return next(iter(languages)) if len(languages) == 1 else "argv"
        if name == "ssh" and not children and len(argv) > 1 and not any(word in {"-V", "-G", "-Q", "-N", "-O"} for word in words):
            return "shell"
        if not children and name in {"sudo", "doas"} and any(
                word in {"--shell", "--login"} or word.startswith("-") and not word.startswith("--")
                and bool(set(word[1:]) & ({"s", "i"} if name == "sudo" else {"s"})) for word in words):
            return "shell"
        if not children and name in {"su", "runuser"}:
            # runuser -u requires a program. Its missing child is not an
            # interactive shell, unlike su and runuser's su-compatible form.
            direct_user = any(word in {"-u", "--user"} or word.startswith("--user=")
                              or word.startswith("-") and not word.startswith("--") and "u" in word[1:]
                              for word in words)
            if any(word in {"-c", "--command", "--session-command"}
                   or word.startswith(("--command=", "--session-command="))
                   or word.startswith("-") and not word.startswith("--") and "c" in word[1:]
                   for word in words):
                return None
            return "shell" if name == "su" or not direct_user else None
        return None
    if name == "xargs":
        return argument_execution_language(argv)
    stdin_paths = {"/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
    if name.startswith("$"):
        return "shell"
    if name == "source" or argv[0] == ".":
        return "shell" if words[:1] and words[0] in stdin_paths else None
    shell = name in {"bash", "sh", "zsh", "dash", "ksh"}
    python = re.fullmatch(r"python[0-9.]*", name) is not None
    if not (shell or python or name == "node"):
        return None
    cursor, force_stdin = 0, False
    while cursor < len(words):
        word = words[cursor]
        if word == "-":
            return "shell" if shell else "python" if python else "node"
        if word == "--":
            cursor += 1
            break
        if not word.startswith(("-", "+")):
            break
        option = word.partition("=")[0]
        if shell:
            if not word.startswith("--") and "c" in word[1:]:
                body = shell_c_body(argv)
                return shell_stdin_language(body) if body is not None else None
            force_stdin |= not word.startswith("--") and "s" in word[1:]
            takes_value = (option in {"--rcfile", "--init-file", "--emulate"}
                           or not word.startswith("--") and word.endswith(("o", "O")))
        elif python:
            if word.startswith(("-c", "-m")):
                return None
            takes_value = option in {"-W", "-X", "--check-hash-based-pycs"}
        else:
            if option in {"-e", "--eval", "-p", "--print"} or word.startswith(("-e", "-p")) and not word.startswith("--"):
                return None
            takes_value = option in {"--input-type", "--require", "-r", "--import", "--loader", "--experimental-loader"}
        cursor += 1 + (takes_value and "=" not in word)
    if cursor < len(words) and not force_stdin:
        if words[cursor] not in stdin_paths:
            return None
    return "shell" if shell else "python" if python else "node"


def forwards_stdin(argv):
    """Whether a pipeline command can carry its input stream to the next reader."""
    argv = unwrapped_command(argv)
    if not argv:
        return False
    name = Path(argv[0]).name
    if name in {"printf", "echo", "true", "false", ":", "cd", "gh", "gh-plan.py", "gh-pr.py"}:
        return False
    if name in {"set", "setopt", "unsetopt", "shopt", "emulate"} and argv[1:]:
        return False  # Shell option changes do not copy stdin to stdout.
    if name == "tee" and any(word in {"--help", "--version"} for word in argv[1:]):
        return False
    if name == "cat":
        if any(word in {"--help", "--version"} for word in argv[1:]):
            return False
        operands = []
        options = True
        for word in argv[1:]:
            if options and word == "--":
                options = False
            elif not options or not word.startswith("-") or word == "-":
                operands.append(word)
        return not operands or "-" in operands
    # Filters and unresolved programs may forward their input. This preserves
    # uncertainty rather than claiming an unknown transformation is inert.
    return True


def node_expression_parts(code, start, end, separators=","):
    """Split actual JS expressions at balanced, unquoted delimiters."""
    parts, stack, begin = [], [], start
    for cursor in range(start, end):
        token = code[cursor]
        if not stack and token in separators:
            parts.append((begin, cursor))
            begin = cursor + 1
        elif token in "([{":
            stack.append(token)
        elif token in ")]}":
            if not stack:
                return [*parts, (begin, cursor)]
            stack.pop()
    return [*parts, (begin, end)]


def node_control_statements(code):
    """Keep branch boundaries while reading mutable process-launch arguments."""
    def skip(cursor):
        while cursor < len(code) and (code[cursor].isspace() or code[cursor] == ";"):
            cursor += 1
        return cursor

    def close(cursor):
        stack = []
        for end in range(cursor, len(code)):
            if code[end] in "([{":
                stack.append(code[end])
            elif code[end] in ")]}":
                if not stack:
                    return end
                stack.pop()
                if not stack:
                    return end
        return len(code)

    def statement(cursor):
        start = cursor = skip(cursor)
        if cursor >= len(code):
            return None
        if code[cursor] == "{":
            children, cursor = sequence(cursor + 1, "}")
            return {"kind": "block", "start": start, "end": cursor, "body": children}
        if re.match(r"try\b", code[cursor:]):
            body = statement(cursor + 3)
            if body is not None:
                end, handler, final = body["end"], None, None
                following = skip(end)
                if re.match(r"catch\b", code[following:]):
                    following = skip(following + 5)
                    if following < len(code) and code[following] == "(":
                        following = close(following) + 1
                    handler = statement(following)
                    end = handler["end"] if handler else end
                following = skip(end)
                if re.match(r"finally\b", code[following:]):
                    final = statement(following + 7)
                    end = final["end"] if final else end
                return {"kind": "try", "start": start, "end": end, "body": [body],
                        "catch": [handler] if handler else [], "finally": [final] if final else []}
        keyword = re.match(r"(if|while|for|switch)\b", code[cursor:])
        if keyword:
            condition = cursor + keyword.end()
            while condition < len(code) and code[condition].isspace():
                condition += 1
            if condition < len(code) and code[condition] == "(":
                finish = close(condition)
                body = statement(finish + 1)
                if body is not None:
                    if keyword[1] == "switch":
                        labels, stack, at = [], [], body["start"] + 1
                        while at < body["end"] - 1:
                            label = re.match(r"(case|default)\b", code[at:]) if not stack else None
                            if label:
                                colon = node_expression_parts(code, at + label.end(), body["end"] - 1, ":")[0][1]
                                labels.append((at, colon + 1, label[1]))
                                at = colon + 1
                                continue
                            if code[at] in "([{":
                                stack.append(code[at])
                            elif code[at] in ")]}":
                                if stack:
                                    stack.pop()
                            at += 1
                        cases = []
                        for index, (label_start, begin, kind) in enumerate(labels):
                            end = labels[index + 1][0] if index + 1 < len(labels) else body["end"] - 1
                            cases.append({"start": label_start, "end": end, "body": sequence(begin, until=end)[0]})
                        return {"kind": "switch", "start": start, "end": body["end"], "cases": cases,
                                "default": any(kind == "default" for _, _, kind in labels)}
                    end, other = body["end"], None
                    following = skip(end)
                    if keyword[1] == "if" and re.match(r"else\b", code[following:]):
                        other = statement(following + 4)
                        if other is not None:
                            end = other["end"]
                    return {"kind": keyword[1], "start": start, "end": end,
                            "condition": (condition + 1, finish), "body": [body],
                            "other": [other] if other else []}
        end = node_expression_parts(code, cursor, len(code), ";\n")[0][1]
        if end <= cursor:
            end = cursor + 1
        elif end < len(code) and code[end] in ";\n":
            end += 1
        return {"kind": "statement", "start": start, "end": end}

    def sequence(cursor, closing=None, until=None):
        nodes = []
        while (cursor := skip(cursor)) < (len(code) if until is None else until):
            if closing and code[cursor] == closing:
                return nodes, cursor + 1
            node = statement(cursor)
            if node is None:
                break
            nodes.append(node)
            cursor = node["end"]
        return nodes, cursor
    return sequence(0)[0]


def node_deferred_functions(code):
    """Find named function bodies whose assignments wait for a code reference."""
    def closing(start):
        depth = 0
        for cursor in range(start, len(code)):
            if code[cursor] == code[start]:
                depth += 1
            elif code[cursor] == {"(": ")", "{": "}"}[code[start]]:
                depth -= 1
                if not depth:
                    return cursor
        return len(code)

    functions = []
    for match in re.finditer(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)?\s*\(", code):
        parameter_end = closing(match.end() - 1)
        body = parameter_end + 1
        while body < len(code) and code[body].isspace():
            body += 1
        if body >= len(code) or code[body] != "{":
            continue
        assigned = re.search(r"([A-Za-z_$][\w$]*)\s*=\s*$", code[:match.start()])
        name = assigned[1] if assigned else match[1]
        if name:
            declaration = assigned.start(1) if assigned else match.start(1)
            functions.append((name, declaration, body + 1, closing(body)))
    for match in re.finditer(r"=>\s*", code):
        binding = re.search(r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*$", code[:match.start()])
        if binding is None:
            continue  # Anonymous callbacks may be invoked by their consumer.
        body = match.end()
        if body < len(code) and code[body] == "{":
            begin, end = body + 1, closing(body)
        else:
            begin, end = node_expression_parts(code, body, len(code), ";,\n")[0]
        functions.append((binding[1], binding.start(1), begin, end))
    return functions


def node_ownership(content):
    """Resolve child_process arguments at their call site and binding time."""
    atoms_pattern = r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`'''
    atoms = list(re.finditer(atoms_pattern, content))
    code, literals = list(content), {}
    for atom in atoms:
        if not atom[0].startswith(("//", "/*")):
            literals[atom.start()] = atom
        code[atom.start():atom.end()] = " " * len(atom[0])
    code = "".join(code)
    declarations = {}
    for definition in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(\+=|=)(?!=|>)", code):
        start = definition.end()
        _, end = node_expression_parts(code, start, len(code), ";,\n")[0]
        declarations.setdefault(definition[1], []).append((definition.start(), start, end, definition[2]))
    statements = node_control_statements(code)
    deferred = node_deferred_functions(code)

    def inactive_body(position, before):
        for name, declaration, begin, finish in deferred:
            if begin <= position < finish and not begin <= before < finish:
                references = (match.start() for match in re.finditer(r"\b" + re.escape(name) + r"\b", code[:before]))
                if not any(at != declaration and not begin <= at < finish for at in references):
                    return True
        return False

    @lru_cache(maxsize=None)
    def bindings(name, before):
        assignments = [item for item in declarations.get(name, []) if not inactive_body(item[0], before)]
        def may_throw(node):
            text = code[node["start"]:node["end"]].strip().rstrip(";").strip()
            if text in {"", "break", "continue"}:
                return False
            writes = [(variable, item) for variable, items in declarations.items() for item in items
                      if node["start"] <= item[0] < node["end"]]
            if len(writes) == 1:
                variable, (position, begin, finish, operator) = writes[0]
                prefix = code[node["start"]:position].strip()
                suffix = code[finish:node["end"]].strip().strip(";").strip()
                immutable = any(re.search(r"\bconst\s*$", code[:item[0]]) for item in declarations.get(variable, []) if item[0] < position)
                if operator == "=" and prefix in {"", "let", "var", "const"} and not suffix and not (immutable and not prefix):
                    literal = content[begin:finish].strip()
                    try:
                        ast.literal_eval(literal)
                    except (ValueError, SyntaxError):
                        return literal not in {"true", "false", "null"}
                    return False
            return True

        def walk(nodes, state, throws=None):
            for node in nodes:
                if node["start"] >= before:
                    break
                if node["kind"] == "statement":
                    if throws is not None and may_throw(node):
                        throws.append(set(state))
                    for assignment in assignments:
                        position = assignment[0]
                        if node["start"] <= position < min(node["end"], before):
                            conditional = re.search(r"&&|\|\||\?", code[node["start"]:position])
                            conditional = conditional or any(begin <= position < finish and not begin <= before < finish
                                                             for _, _, begin, finish in deferred)
                            state = state | {assignment} if conditional else {assignment}
                    if re.match(r"\s*throw\b", code[node["start"]:node["end"]]):
                        return set()
                elif node["kind"] == "block":
                    state = walk(node["body"], state, throws)
                elif node["kind"] == "switch":
                    cases = node["cases"]
                    active = next((index for index, case in enumerate(cases) if case["start"] <= before < case["end"]), None)
                    merged = set(state) if active is None and not node["default"] else set()
                    for entry in range(len(cases) if active is None else active + 1):
                        current = set(state)
                        for index in range(entry, len(cases)):
                            stopped = False
                            for child in cases[index]["body"]:
                                if child["start"] >= before:
                                    break
                                current = walk([child], current, throws)
                                if child["kind"] == "statement" and re.match(r"\s*break\b", code[child["start"]:child["end"]]):
                                    stopped = True
                                    break
                            if stopped or active is not None and index == active:
                                if stopped and active is not None and index < active:
                                    current = set()
                                break
                        merged.update(current)
                    state = merged
                elif node["kind"] == "try":
                    exceptional = []
                    normal = walk(node["body"], set(state), exceptional)
                    if node["catch"] and before >= node["catch"][0]["start"]:
                        caught = set().union(*exceptional) if exceptional else set()
                        caught = walk(node["catch"], caught, throws) if exceptional else set()
                        normal = caught if before < node["catch"][0]["end"] else normal | caught
                    elif node["finally"] and node["finally"][0]["start"] <= before < node["finally"][0]["end"]:
                        normal |= set().union(*exceptional) if exceptional else set()
                    elif throws is not None:
                        throws.extend(exceptional)
                    state = walk(node["finally"], normal, throws)
                else:
                    body, other = node["body"], node["other"]
                    if before <= body[0]["start"]:
                        break
                    if before < body[0]["end"]:
                        return walk(body, state, throws)
                    if other and other[0]["start"] <= before < other[0]["end"]:
                        return walk(other, state, throws)
                    condition = code[slice(*node["condition"])].strip()
                    if condition == "false":
                        state = walk(other, state, throws)
                    elif condition == "true" and node["kind"] == "if":
                        state = walk(body, state, throws)
                    else:
                        if throws is not None:
                            throws.append(set(state))
                        state = walk(body, state, throws) | walk(other, state, throws)
            return state
        return frozenset(walk(statements, set()))

    def evidence(start, end, before, seen=frozenset()):
        """Unresolved launch operands retain only their own bound source text."""
        pieces, fragment = [], list(content[start:end])
        for literal in re.finditer(atoms_pattern, content[start:end]):
            if not literal[0].startswith(("//", "/*")):
                try:
                    text = ast.literal_eval(literal[0])
                except (ValueError, SyntaxError):
                    text = literal[0]
                pieces.append(text if isinstance(text, str) else literal[0])
                if literal[0].startswith('`'):
                    for interpolation in re.finditer(r"\$\{([^}]+)\}", literal[0]):
                        position = start + literal.start()
                        pieces.extend(evidence(position + interpolation.start(1), position + interpolation.end(1), before, seen))
            fragment[literal.start():literal.end()] = " " * len(literal[0])
        words = re.findall(r"[A-Za-z_$][\w$]*", "".join(fragment))
        for word in words:
            for position, begin, finish, operator in bindings(word, before):
                key = (word, position)
                if key not in seen:
                    pieces.extend(evidence(begin, finish, position, seen | {key}))
                    if operator == "+=":
                        pieces.extend(evidence(start, end, position, seen | {key}))
        return pieces

    def value(start, end, before, seen=frozenset()):
        expression = content[start:end]
        start += len(expression) - len(expression.lstrip())
        end -= len(expression) - len(expression.rstrip())
        expression = content[start:end]
        identifier = re.fullmatch(r"[A-Za-z_$][\w$]*", expression)
        if identifier:
            preceding = bindings(expression, before)
            if preceding:
                possible = []
                for position, begin, finish, operator in preceding:
                    key = (expression, position)
                    if key in seen:
                        return None
                    assigned = value(begin, finish, position, seen | {key})
                    if operator == "+=":
                        previous = value(start, end, position, seen | {key})
                        assigned = str(previous) + str(assigned) if isinstance(previous, (str, int, float)) and isinstance(assigned, (str, int, float)) else None
                    possible.append(assigned)
                return possible[0] if all(item == possible[0] for item in possible) else None
            return {"true": True, "false": False, "null": None}.get(expression)
        literal = literals.get(start)
        if literal is not None and literal.end() == end:
            if expression.startswith('`'):
                result, cursor = "", start + 1
                for interpolation in re.finditer(r"\$\{([^}]+)\}", expression):
                    result += content[cursor:start + interpolation.start()]
                    operand = value(start + interpolation.start(1), start + interpolation.end(1), before, seen)
                    if not isinstance(operand, (str, int, float)):
                        return None
                    result += str(operand)
                    cursor = start + interpolation.end()
                return result + content[cursor:end - 1]
            try:
                return ast.literal_eval(expression)
            except (ValueError, SyntaxError):
                return None
        parts = node_expression_parts(code, start, end, "+")
        if len(parts) > 1:
            operands = [value(begin, finish, before, seen) for begin, finish in parts]
            if all(isinstance(operand, (str, int, float)) for operand in operands):
                return "".join(str(operand) for operand in operands)
            return None
        if expression.startswith('[') and expression.endswith(']'):
            return [value(begin, finish, before, seen)
                    for begin, finish in node_expression_parts(code, start + 1, end - 1)
                    if content[begin:finish].strip()]
        if expression.startswith('{') and expression.endswith('}'):
            options = {}
            for begin, finish in node_expression_parts(code, start + 1, end - 1):
                property_text = content[begin:finish].strip()
                if not property_text:
                    continue
                if property_text.startswith("..."):
                    spread_start = begin + content[begin:finish].index("...") + 3
                    spread = value(spread_start, finish, before, seen)
                    options.update(spread if isinstance(spread, dict) else {"shell": None})
                    continue
                pair = node_expression_parts(code, begin, finish, ":")
                key_fragment = content[pair[0][0]:pair[0][1]]
                key_text = key_fragment.strip()
                if key_text.startswith("[") and key_text.endswith("]"):
                    key_start = pair[0][0] + key_fragment.index("[") + 1
                    key = value(key_start, pair[0][0] + key_fragment.rindex("]"), before, seen)
                else:
                    key = value(*pair[0], before, seen) if key_text.startswith(("'", '"')) else key_text
                if not isinstance(key, str):
                    options["shell"] = None
                    continue
                options[key] = value(*pair[1], before, seen) if len(pair) > 1 else value(begin, finish, before, seen)
            return options
        if expression.startswith('(') and expression.endswith(')'):
            return value(start + 1, end - 1, before, seen)
        try:
            return ast.literal_eval(expression)
        except (ValueError, SyntaxError):
            return None

    module = r"['\"](?:node:)?child_process['\"]"
    require = rf"\brequire\s*\(\s*{module}\s*\)"
    namespaces, functions = set(), {}
    # Bind actual imports, not declarations quoted inside a fixture/comment.
    for binding in re.finditer(rf"\b(?:const|let|var)\s+(\w+)\s*=\s*{require}|\bimport\s+(?:\*\s+as\s+)?(\w+)\s+from\s+{module}", content):
        if code[binding.start():binding.start() + 5].strip():
            namespaces.add(binding[1] or binding[2])
    for binding in re.finditer(rf"\b(?:const|let|var)\s*\{{([^{{}}]+)\}}\s*=\s*{require}|\bimport\s*\{{([^{{}}]+)\}}\s+from\s+{module}", content):
        if not code[binding.start():binding.start() + 5].strip():
            continue
        for member in (binding[1] or binding[2]).split(","):
            names = re.split(r"\s*(?::|\bas\b)\s*", member.strip())
            if names and names[0] in {"exec", "execSync", "spawn", "spawnSync", "execFile", "execFileSync"}:
                functions[names[-1]] = names[0]
    names = {"exec", "execSync", "spawn", "spawnSync", "execFile", "execFileSync", *functions}
    for call in re.finditer(r"\b(" + "|".join(re.escape(name) for name in sorted(names)) + r")\s*\(", code):
        prefix = content[:call.start()]
        owner = re.search(r"\b(\w+)\s*\.\s*$", prefix)
        direct = re.search(require + r"\s*\.\s*$", prefix)
        if not (direct or owner and owner[1] in namespaces
                or not owner and not prefix.rstrip().endswith(".") and call[1] in functions):
            continue
        kind = call[1] if direct or owner else functions[call[1]]
        parts = node_expression_parts(code, call.end(), len(code))
        command = value(*parts[0], call.start())
        if not isinstance(command, str):
            relevant = parts[:1] if kind in {"exec", "execSync"} else parts[:2]
            related = " ".join(piece for begin, finish in relevant for piece in evidence(begin, finish, call.start()))
            if OWNERSHIP_TEXT.search(related):
                return True
            continue
        if kind in {"exec", "execSync"}:
            if executable_content(command) or opaque_ownership("exec_command", {"cmd": command}):
                return True
        else:
            arguments = value(*parts[1], call.start()) if len(parts) > 1 else []
            options = arguments if isinstance(arguments, dict) else value(*parts[2], call.start()) if len(parts) > 2 else {}
            if isinstance(arguments, dict):
                arguments = []
            shell = options.get("shell", False) if isinstance(options, dict) else None
            if shell is True or isinstance(shell, str) and shell:
                shell_command = " ".join([command, *(str(word) for word in arguments)]) if isinstance(arguments, list) else command
                if executable_content(shell_command) or opaque_ownership("exec_command", {"cmd": shell_command}):
                    return True
            elif shell is None and OWNERSHIP_TEXT.search(command):
                return True
            if not isinstance(arguments, list):
                related = " ".join([command, *evidence(*parts[1], call.start())]) if len(parts) > 1 else command
                if (Path(command).name in {"gh-plan.py", "gh-pr.py", "gh"} or executable_wrapper([command])) and OWNERSHIP_TEXT.search(related):
                    return True
                continue
            argv = [command, *(word if isinstance(word, str) else "$NODE_ARGUMENT" for word in arguments)]
            helper, action = helper_action(argv)
            if ownership_command(argv) or helper in {"gh-plan.py", "gh-pr.py"} and action[:1] == ["$NODE_ARGUMENT"]:
                return True
    return False


def compound_bounds(tokens):
    """Pair existing shell grouping tokens once for parsing and flow analysis."""
    opens = {"(": ")", "{": "}", "while": "done", "until": "done", "for": "done",
             "select": "done", "if": "fi", "case": "esac"}
    stack, bounds = [], {}
    command_start = True
    for index, token in enumerate(tokens):
        if token in opens and (token in {"(", "{"} or command_start):
            stack.append((index, opens[token]))
        elif stack and token == stack[-1][1]:
            begin, _ = stack.pop()
            bounds[begin] = index
        command_start = bool(token and all(c in ";&|\n" for c in token) or token in {"do", "then", "else"})
    return bounds


def compound_stdin_language(tokens, source, at, spans, heredocs):
    """Read a group's actual commands, rather than treating its closing word as argv."""
    bounds = next(((begin, end) for begin, end in compound_bounds(tokens).items() if at in {begin, end}), None)
    if bounds is None:
        return None
    begin, end = bounds
    if begin == at and heredocs and end > min(heredocs):
        return None  # The redirection belongs to an inner command, not the group.
    if begin + 1 >= end or spans[begin + 1][0] is None or spans[end][0] is None:
        return None
    inner = source[spans[begin + 1][0]:spans[end][0]]
    return shell_stdin_language(inner, case=tokens[begin] == "case")


def shell_stdin_language(source, *, case=False):
    """Classify commands connected to a shell body's inherited stdin."""
    lexer = shell_lexer(source, posix=False)
    lexer.whitespace_split = True
    commands, kinds = [], set()
    heredoc_languages(list(lexer), lexer.instream.getvalue(), command_kinds=commands, stdin_kinds=kinds)
    words = [unwrapped_command(argv) for _, argv in commands]
    # A read/eval loop executes the stream line by line, even though eval
    # itself does not read stdin. Ordinary data commands in groups stay data.
    if any(argv[:1] == ["read"] for argv in words) and any(
            argv[:1] == ["eval"] or shell_c_body(argv) and "$" in shell_c_body(argv) for argv in words):
        kinds.add("shell")
    if case:
        for argv in words:
            if ")" in argv:
                kind = stdin_language(argv[argv.index(")") + 1:])
                if kind:
                    kinds.add(kind)
    return next(iter(kinds)) if len(kinds) == 1 else "argv" if kinds else None


def stream_argv(argv):
    """Reuse launcher operands for stream producers and forwarders."""
    for _ in range(16):
        child = unwrapped_command(argv)
        commands = list(executor_commands(child))
        if len(commands) != 1:
            return child
        try:
            following = shlex.split(commands[0])
        except ValueError:
            return child
        if following == child:
            return child
        argv = following
    return child


def heredoc_languages(tokens, source, *, file_commands=None, command_kinds=None, inline_inputs=None,
                      stream_sources=None, output_sources=None, execution_events=None, stdin_kinds=None):
    """Parse command descriptors once and carry each stdin source through groups.

    A balanced group is one outer pipeline node. Its children share the same
    parser, so inner separators and redirections cannot be borrowed by siblings.
    Source sets preserve distinct bodies without concatenating unrelated data.
    """
    spans, cursor = [], 0
    for token in tokens:
        start = source.find(token, cursor)
        spans.append((start, start + len(token)) if start >= 0 else (None, None))
        if start >= 0:
            cursor = start + len(token)
    bounds = compound_bounds(tokens)
    stream_sources = {} if stream_sources is None else stream_sources
    languages = {}

    def parse(begin, end):
        segments, current, index = [], [], begin
        while index < end:
            token = tokens[index]
            if token in {"(", "{"} and index in bounds and bounds[index] < end:
                close = bounds[index]
                current.extend(range(index, close + 1))
                index = close + 1
                continue
            if token and all(c in ";&|\n" for c in token):
                if current:
                    segments.append((current, token))
                current = []
            else:
                current.append(index)
            index += 1
        if current:
            segments.append((current, ""))
        nodes = []
        for segment, separator in segments:
            argv, heredocs, children, cursor, last_word = [], [], None, 0, None
            substitutions = {}
            descriptors = {0: "pipe-in", 1: "pipe-out", 2: None}
            while cursor < len(segment):
                index, token = segment[cursor], tokens[segment[cursor]]
                if (token in {"<", ">"} and cursor + 1 < len(segment)
                        and tokens[segment[cursor + 1]] == "(" and segment[cursor + 1] in bounds
                        and spans[index][1] == spans[segment[cursor + 1]][0]):
                    opening = segment[cursor + 1]
                    close = bounds[opening]
                    placeholder = f"PROCESS_SUBSTITUTION_{index}"
                    substitutions[placeholder] = (token, parse(opening + 1, close))
                    argv.append(placeholder)
                    cursor = segment.index(close) + 1
                    continue
                if token in {"(", "{"} and index in bounds and bounds[index] in segment:
                    close = bounds[index]
                    children = parse(index + 1, close)
                    cursor = segment.index(close) + 1
                    continue
                if token in {"<", ">", "<<", ">>", "<<<", "<>", "<&", ">&", ">|", "&>", "&>>"}:
                    fd = 0 if token.startswith("<") else 1
                    previous = segment[cursor - 1] if cursor else None
                    if (previous == last_word and previous is not None and tokens[previous].isdigit()
                            and spans[previous][1] == spans[index][0]):
                        fd = int(tokens[previous])
                        argv.pop()
                    operand = tokens[segment[cursor + 1]] if cursor + 1 < len(segment) else ""
                    if (cursor + 2 < len(segment) and operand in {"<", ">"}
                            and tokens[segment[cursor + 2]] == "(" and segment[cursor + 2] in bounds
                            and spans[segment[cursor + 1]][1] == spans[segment[cursor + 2]][0]):
                        opening = segment[cursor + 2]
                        close = bounds[opening]
                        descriptors[fd] = ("process-input" if fd == 0 else "process-output", parse(opening + 1, close))
                        cursor = segment.index(close) + 1
                        continue
                    if token in {"<<", "<<<"}:
                        heredocs.append(index)
                        languages[index] = None
                        descriptors[fd] = index
                        if token == "<<<" and inline_inputs is not None:
                            decoded = shlex.split(operand, comments=False)
                            if len(decoded) == 1:
                                inline_inputs[index] = (operand, decoded[0] + "\n")
                    elif token in {"<&", ">&"} and operand.isdigit():
                        descriptors[fd] = descriptors.get(int(operand))
                    elif token == "<":
                        target = shlex.split(operand, comments=False)
                        descriptors[fd] = ("input-file", target[0]) if len(target) == 1 else None
                    elif token in {">", ">>", ">|", "&>", "&>>"}:
                        target = shlex.split(operand, comments=False)
                        descriptors[fd] = ("file", target[0], token not in {">>", "&>>"}) if len(target) == 1 else None
                        if token.startswith("&>"):
                            descriptors[2] = descriptors[fd]
                    else:
                        descriptors[fd] = None
                    cursor += 2
                    continue
                argv.extend(shlex.split(token, comments=False))
                last_word = index
                cursor += 1
            language = stdin_language(argv)
            if children is None and tokens[segment[0]] in {"}", ")", "done", "fi", "esac"}:
                language = compound_stdin_language(tokens, source, segment[0], spans, heredocs) or language
            nodes.append({"argv": argv, "children": children, "language": language, "index": segment[0],
                          "descriptors": descriptors, "heredocs": heredocs,
                          "substitutions": substitutions,
                          "separator": separator, "source": " ".join(tokens[index] for index in segment)})
        return nodes

    def file_sources(identifier):
        stream = stream_sources.get(identifier, {})
        if stream.get("kind") == "file":
            yield stream["path"]
        elif stream.get("kind") in {"choice", "concat"}:
            for part in stream["parts"]:
                yield from file_sources(part)

    def assign_language(sources, language, position=None):
        if language:
            if stdin_kinds is not None and "external" in sources:
                stdin_kinds.add(language)
            for identifier in sources:
                if identifier != "external":
                    previous = languages.get(identifier)
                    languages[identifier] = language if previous in {None, language} else "argv"
            if execution_events is not None:
                paths = [path for identifier in sources for path in file_sources(identifier)]
                if paths:
                    execution_events.append({"kind": "read", "position": position, "paths": paths})

    def record_file(node, target, sources, forwards, replaces=True):
        if file_commands is not None:
            identifiers = sorted((identifier for identifier in sources if identifier != "external"), key=repr)
            for identifier in identifiers or [None]:
                file_commands.append({"target": target, "body": identifier,
                                      "argv": node["argv"], "source": node["source"],
                                      "position": node["index"],
                                      "forwards": forwards, "replaces": replaces})

    def append_streams(before, after):
        """Concatenate actual sequential bytes, keeping variants as alternatives."""
        if not before or not after:
            return set(before or after)
        def choice(sources):
            if len(sources) == 1:
                return next(iter(sources))
            parts = tuple(sorted(sources, key=repr))
            identifier = ("choice", parts)
            stream_sources[identifier] = {"kind": "choice", "parts": parts}
            return identifier
        parts = (choice(before), choice(after))
        identifier = ("concat", parts)
        stream_sources[identifier] = {"kind": "concat", "parts": parts}
        return {identifier}

    def flow(nodes, inherited):
        output, pipe, previous = set(), set(), ""
        for node in nodes:
            descriptors = node["descriptors"]
            incoming = pipe if previous in {"|", "|&"} else inherited
            input_source = descriptors[0]
            inputs = set(incoming) if input_source == "pipe-in" else {input_source} if isinstance(input_source, int) else set()
            if isinstance(input_source, tuple) and input_source[0] == "input-file":
                identifier = ("file", input_source[1])
                stream_sources[identifier] = {"kind": "file", "path": input_source[1]}
                inputs.add(identifier)
            if isinstance(input_source, tuple) and input_source[0] == "process-input":
                inputs = flow(input_source[1], inherited)
            if node["children"] is not None:
                produced = flow(node["children"], inputs)
                forwards = bool(produced)
            else:
                assign_language(inputs, node["language"], node["index"])
                child = stream_argv(node["argv"])
                if execution_events is not None:
                    execution_events.append({"kind": "read", "position": node["index"], "paths": execution_targets(node["argv"])})
                substitution_outputs = {}
                for placeholder, (direction, commands) in node["substitutions"].items():
                    if direction == "<":
                        substitution_outputs[placeholder] = flow(commands, inherited)
                        language = stdin_language(["/dev/stdin" if word == placeholder else word for word in node["argv"]])
                        assign_language(substitution_outputs[placeholder], language, node["index"])
                forwards = forwards_stdin(child)
                produced = set(inputs) if forwards else set()
                variants = literal_producer_variants(child)
                if variants is not None:
                    produced = set()
                    for variant, content in enumerate(variants):
                        identifier = ("literal", source, node["index"], variant)
                        stream_sources[identifier] = {"kind": "literal", "content": content}
                        if len(variants) > 1:
                            explicit_shell = any(Path(word).name in {"bash", "sh", "zsh", "dash", "ksh"} for word in node["argv"])
                            stream_sources[identifier].update(echo_mode=variant,
                                echo_scope=("child", source, node["index"]) if explicit_shell else node["echo_scope"])
                        produced.add(identifier)
                    forwards = True
                if child and Path(child[0]).name == "cat" and not any(word in {"--help", "--version"} for word in child[1:]):
                    operands, options = [], True
                    for word in child[1:]:
                        if options and word == "--":
                            options = False
                        elif not options or not word.startswith("-") or word == "-":
                            operands.append(word)
                    if operands:
                        produced = set()
                    for path in operands:
                        if path == "-":
                            produced = append_streams(produced, inputs)
                        elif path in substitution_outputs:
                            produced = append_streams(produced, substitution_outputs[path])
                        else:
                            identifier = ("file", path)
                            stream_sources[identifier] = {"kind": "file", "path": path}
                            produced = append_streams(produced, {identifier})
                    forwards |= bool(produced)
                body = shell_c_body(child)
                if child and Path(child[0]).name == "eval":
                    body = " ".join(child[1:])
                if body is not None:
                    expansion = re.fullmatch(r"\s*\$\(([\s\S]*)\)\s*", body)
                    if expansion:
                        lexer = shell_lexer(expansion[1], posix=False)
                        lexer.whitespace_split = True
                        substitution_sources = set()
                        heredoc_languages(list(lexer), lexer.instream.getvalue(), stream_sources=stream_sources,
                                          output_sources=substitution_sources)
                        assign_language(substitution_sources, "shell", node["index"])
                if child and Path(child[0]).name == "tee" and forwards:
                    options = True
                    for word in child[1:]:
                        if options and word == "--":
                            options = False
                        elif not options or not word.startswith("-") or word == "-":
                            if word in node["substitutions"] and node["substitutions"][word][0] == ">":
                                flow(node["substitutions"][word][1], inputs)
                            else:
                                record_file(node, word, inputs, True, not any(flag in {"-a", "--append"} for flag in child[1:]))
            if isinstance(descriptors[1], tuple) and descriptors[1][0] == "file":
                record_file(node, descriptors[1][1], produced, forwards, descriptors[1][2])
            elif isinstance(descriptors[1], tuple) and descriptors[1][0] == "process-output":
                flow(descriptors[1][1], produced)
            outgoing = produced if descriptors[1] == "pipe-out" else set()
            previous = node["separator"]
            if previous in {"|", "|&"}:
                pipe = outgoing
            else:
                output = append_streams(output, outgoing)
        return output

    nodes = parse(0, len(tokens))
    def echo_scopes(nodes, scope):
        """Share one unknown echo mode until an actual mutation or child shell."""
        for node in nodes:
            node["echo_scope"] = scope
            child = stream_argv(node["argv"])
            if child:
                name, options = Path(child[0]).name, child[1:]
                setting_options = options[:options.index("--")] if "--" in options else options
                if (name in {"source", "."}
                        or name == "emulate" and any(option in {"sh", "zsh", "ksh", "csh"} for option in options)
                        and not any(re.fullmatch(r"-[A-Za-z]*l[A-Za-z]*", option) for option in setting_options)
                        or name in {"setopt", "unsetopt"} and any(option.lower().replace("_", "") in {"bsdecho", "nobsdecho"} for option in options)
                        or name == "shopt" and "xpg_echo" in options
                        and any(re.fullmatch(r"-[A-Za-z]*[su][A-Za-z]*", option) for option in setting_options)
                        or name == "set" and any(re.fullmatch(r"[-+][A-Za-z]*o", option) and following.lower().replace("_", "") in {"posix", "bsdecho", "nobsdecho"}
                                                 for option, following in zip(setting_options, setting_options[1:]))):
                    scope = ("mode", source, node["index"])
            if node["children"] is not None:
                following = echo_scopes(node["children"], scope)
                if tokens[node["index"]] != "(":
                    scope = following
            for placeholder, (_, commands) in node["substitutions"].items():
                echo_scopes(commands, ("process", source, node["index"], placeholder))
            for descriptor, value in node["descriptors"].items():
                if isinstance(value, tuple) and value[0] in {"process-input", "process-output"}:
                    echo_scopes(value[1], ("descriptor", source, node["index"], descriptor))
        return scope
    echo_scopes(nodes, ("native", source))
    produced = flow(nodes, {"external"})
    if output_sources is not None:
        output_sources.update(identifier for identifier in produced if identifier != "external")
    if command_kinds is not None:
        def collect(nodes):
            for node in nodes:
                if node["children"] is not None:
                    collect(node["children"])
                else:
                    command_kinds.append((node["language"], node["argv"]))
        collect(nodes)
    return languages


def producer_escape(text, cursor, *, mode):
    """Decode one shell producer escape, preserving unknown escapes literally."""
    start = cursor
    cursor += 1
    if cursor == len(text):
        return "\\", cursor, False
    char = text[cursor]
    escapes = dict(zip("\\abefnrtvE", "\\\a\b\x1b\f\n\r\t\v\x1b"))
    if char in escapes:
        return escapes[char], cursor + 1, False
    if char == "c" and mode != "format":
        return "", cursor + 1, True
    digits, base, limit = "", 0, 0
    if char == "x":
        cursor, base, limit = cursor + 1, 16, 2
    elif char in {"u", "U"}:
        cursor, base, limit = cursor + 1, 16, 4 if char == "u" else 8
    elif char in "01234567" and (mode != "echo" or char == "0"):
        base, limit = 8, 3
        if char == "0" and mode != "format":
            cursor += 1
    if base:
        valid = "0123456789abcdefABCDEF" if base == 16 else "01234567"
        while cursor < len(text) and text[cursor] in valid and len(digits) < limit:
            digits += text[cursor]
            cursor += 1
        if digits:
            value = int(digits, base)
            if value <= 0x10ffff:
                return chr(value), cursor, False
        elif base == 8:
            return "\0", cursor, False
    return text[start:cursor + 1] if cursor == start + 1 else text[start:cursor], cursor + (cursor == start + 1), False


def producer_escapes(text, *, mode):
    pieces, cursor = [], 0
    while cursor < len(text):
        if text[cursor] == "\\":
            piece, cursor, stopped = producer_escape(text, cursor, mode=mode)
            pieces.append(piece)
            if stopped:
                return "".join(pieces), True
        else:
            pieces.append(text[cursor])
            cursor += 1
    return "".join(pieces), False


def literal_producer_output(argv, fallback):
    """Resolve producer output without promoting their quoted shell operands."""
    argv = unwrapped_command(argv)
    if not argv:
        return fallback
    if Path(argv[0]).name == "echo":
        cursor, escapes, newline = 1, False, True
        while cursor < len(argv) and re.fullmatch(r"-[neE]+", argv[cursor]):
            for flag in argv[cursor][1:]:
                if flag == "n":
                    newline = False
                else:
                    escapes = flag == "e"
            cursor += 1
        content = " ".join(argv[cursor:])
        content, stopped = producer_escapes(content, mode="echo") if escapes else (content, False)
        return content + ("\n" if newline and not stopped else "")
    if Path(argv[0]).name != "printf" or len(argv) < 2:
        return fallback
    words = argv[2:] if argv[1] == "--" else argv[1:]
    if not words:
        return ""
    format, operands, used, pieces = words[0], words[1:], 0, []
    while True:
        cursor, before = 0, used
        while cursor < len(format):
            char = format[cursor]
            if char == "\\":
                piece, cursor, _ = producer_escape(format, cursor, mode="format")
            elif char == "%":
                conversion = format[cursor + 1:cursor + 2]
                cursor += 2
                if conversion == "%":
                    piece = "%"
                elif conversion in {"s", "b", "q"}:
                    piece = operands[used] if used < len(operands) else ""
                    used += 1
                    if conversion == "b":
                        piece, stopped = producer_escapes(piece, mode="argument")
                        if stopped:
                            return "".join([*pieces, piece])
                    elif conversion == "q":
                        piece = shlex.quote(piece)
                else:
                    # An unsupported format cannot prove its output inert.
                    # Retain executable operands as candidate file contents.
                    return "\n".join([format, " ".join(operands), *operands])
            else:
                piece, cursor = char, cursor + 1
            pieces.append(piece)
        if used == before or used >= len(operands):
            return "".join(pieces)


def literal_producer_variants(argv):
    """Keep possible literal output streams separate when shell defaults differ."""
    argv = unwrapped_command(argv)
    if not argv or Path(argv[0]).name not in {"echo", "printf"}:
        return None
    outputs = [literal_producer_output(argv, "")]
    if Path(argv[0]).name == "echo":
        cursor, explicit = 1, False
        while cursor < len(argv) and re.fullmatch(r"-[neE]+", argv[cursor]):
            explicit |= bool(set(argv[cursor][1:]) & {"e", "E"})
            cursor += 1
        if not explicit:
            # bash's default echo leaves escapes literal; zsh's expands them.
            # Native tool history need not record its shell's option state.
            outputs.append(literal_producer_output([argv[0], "-e", *argv[1:]], ""))
    return tuple(dict.fromkeys(outputs))


class UnresolvedStream(str):
    """Bounded stream expansion retained as uncertainty, never inert text."""

    def __new__(cls, possible_ownership):
        result = super().__new__(cls, "")
        result.possible_ownership = possible_ownership
        return result


def stdin_ownership(content, language):
    """Use the same execution check for heredoc and here-string input streams."""
    if isinstance(content, UnresolvedStream):
        return language is not None and content.possible_ownership
    if language == "shell":
        return executable_content(content) or opaque_ownership("exec_command", {"cmd": content})
    if language == "python":
        return python_ownership(content)
    if language == "node":
        return node_ownership(content)
    return language is not None and bool(OWNERSHIP_TEXT.search(content))


def shell_code(text, *, writes=None, reads=None, replacements=None, effects=None):
    """Extract bodies first, then resolve streams across the complete command."""
    # Quoted cat substitutions are a common CLI body argument. Their literal
    # contents must never participate in either shell lexing or '&' detection.
    substitution = re.compile(r"\$\(cat[ \t]+<<(-?)(['\"])([\w-]+)\2[ \t]*\n([\s\S]*?)\n[\t]*\3\r?\n\s*\)")
    text = substitution.sub("inert-heredoc-body", text)
    code, pending, header = [], [], ""
    bodies = []
    for line in text.splitlines(keepends=True):
        if pending:
            delimiter, tabs, expands, body = pending[0]
            if (line.lstrip("\t") if tabs else line).rstrip("\r\n") == delimiter:
                bodies.append(("".join(body), expands))
                pending.pop(0)
            else:
                body.append(line)
            continue
        header += line
        lexer = shell_lexer(header, posix=False)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            continue  # A quoted word may span several physical lines.
        if len(line.rstrip("\r\n")) - len(line.rstrip("\r\n").rstrip("\\")) & 1:
            continue  # Complete an escaped newline before reading delimiters.
        for index, token in enumerate(tokens):
            if token != "<<":
                continue
            if index + 1 == len(tokens):
                raise ValueError("unparsed heredoc delimiter")
            raw = tokens[index + 1]
            tabs = raw.startswith("-")
            delimiter = shlex.split(raw[1:] if tabs else raw, comments=False)
            if len(delimiter) != 1:
                raise ValueError("unparsed heredoc delimiter")
            pending.append((delimiter[0], tabs, not any(c in raw for c in "'\"\\"), []))
        code.append(header)
        header = ""
    if pending or header:
        raise ValueError("unparsed heredoc body")
    code = "".join(code)
    lexer = shell_lexer(code, posix=False)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    tokens = list(lexer)
    file_commands, inline_inputs, stream_sources = [], {}, {}
    execution_events = [] if effects is not None else None
    languages = heredoc_languages(tokens, lexer.instream.getvalue(), file_commands=file_commands,
                                  inline_inputs=inline_inputs, stream_sources=stream_sources, execution_events=execution_events)
    contents, owns = {}, False
    identifiers = [index for index, token in enumerate(tokens) if token == "<<"]
    if len(identifiers) != len(bodies):
        raise ValueError("unparsed heredoc correspondence")
    for identifier, (content, expands) in zip(identifiers, bodies):
        if expands:
            owns |= any(executable_content(expansion) or opaque_ownership("exec_command", {"cmd": expansion})
                        for expansion in shell_expansions(content, heredoc=True))
            content = re.sub(r"\\([\\$`\n])", lambda match: "" if match[1] == "\n" else match[1], content)
        contents[identifier] = content
    for identifier, (raw, content) in inline_inputs.items():
        owns |= any(executable_content(expansion) or opaque_ownership("exec_command", {"cmd": expansion})
                    for expansion in shell_expansions(raw))
        contents[identifier] = content
    def possible_substring(identifier, needle):
        @lru_cache(maxsize=None)
        def states(identifier, matched):
            if matched == len(needle):
                return frozenset({matched})
            stream = stream_sources.get(identifier, {})
            raw = contents.get(identifier, stream.get("content"))
            if raw is not None:
                for char in raw:
                    prefix = needle[:matched] + char
                    matched = next(length for length in range(min(len(needle), len(prefix)), -1, -1)
                                   if prefix.endswith(needle[:length]))
                    if matched == len(needle):
                        break
                return frozenset({matched})
            if stream.get("kind") == "choice":
                return frozenset(value for part in stream["parts"] for value in states(part, matched))
            if stream.get("kind") == "concat":
                current = frozenset({matched})
                for part in stream["parts"]:
                    current = frozenset(value for previous in current for value in states(part, previous))
                return current
            return frozenset({len(needle)})  # Unknown file bytes stay conservative.
        return len(needle) in states(identifier, 0)

    def unresolved(identifier):
        computed = ("exec", "spawn", "system", "subprocess", "child_process", "eval", "$", "`", "python", "node")
        possible = any(possible_substring(identifier, word) for word in computed)
        possible |= (possible_substring(identifier, "gh-plan.py") and possible_substring(identifier, "claim")
                     or possible_substring(identifier, "gh-pr.py") and possible_substring(identifier, "create")
                     or possible_substring(identifier, "gh") and possible_substring(identifier, "pr") and possible_substring(identifier, "create"))
        return UnresolvedStream(possible)

    resolved, expansion_limit = {}, object()
    def content_of(identifier):
        if identifier in resolved:
            return resolved[identifier]
        stream = stream_sources.get(identifier, {})
        raw = contents.get(identifier, stream.get("content"))
        if raw is not None:
            bindings = frozenset({(stream["echo_scope"], stream["echo_mode"])}) if "echo_scope" in stream else frozenset()
            result = ((raw, bindings),)
        elif stream.get("kind") in {"concat", "choice"}:
            parts = stream["parts"]
            children = [content_of(part) for part in parts]
            if any(child is None for child in children):
                result = None
            elif any(child is expansion_limit for child in children):
                result = expansion_limit
            else:
                variants = set()
                if stream["kind"] == "choice":
                    variants.update(value for child in children for value in child)
                else:
                    for left, left_bindings in children[0]:
                        for right, right_bindings in children[1]:
                            if any(scope == other_scope and mode != other_mode
                                   for scope, mode in left_bindings for other_scope, other_mode in right_bindings):
                                continue
                            variants.add((left + right, left_bindings | right_bindings))
                            if len(variants) > 64:
                                break
                        if len(variants) > 64:
                            break
                result = tuple(variants) if len(variants) <= 64 else expansion_limit
        else:
            result = None
        resolved[identifier] = result
        return result

    def variants_of(identifier):
        choices = content_of(identifier)
        if choices is expansion_limit:
            return unresolved(identifier)
        if choices is None:
            return None
        return tuple(dict.fromkeys(value for value, _ in choices))

    def file_parts(identifier):
        stream = stream_sources.get(identifier, {})
        if stream.get("kind") == "file":
            yield stream["path"]
        elif stream.get("kind") in {"concat", "choice"}:
            for part in stream["parts"]:
                yield from file_parts(part)

    for identifier in languages:
        if reads is not None and languages.get(identifier):
            reads.update(file_parts(identifier))
    if writes is not None or effects is not None:
        for command in file_commands:
            input_source = command["body"]
            variants = variants_of(input_source) if input_source is not None else None
            if variants is not None and command.get("forwards", forwards_stdin(command["argv"])):
                variants = (variants,) if isinstance(variants, UnresolvedStream) else variants
            else:
                # Literal producers write their operands, rather than the
                # shell command that quoted those operands as data. A replaced
                # or unused stdin cannot lend a heredoc body to that output.
                variants = (literal_producer_output(command["argv"], command["source"]),)
            if writes is not None:
                writes.extend((command["target"], content) for content in variants)
            if effects is not None:
                execution_events.append({"kind": "write", "position": command["position"],
                                         "path": command["target"], "contents": variants,
                                         "replaces": command["replaces"]})
            if replacements is not None and command["replaces"]:
                replacements.add(command["target"])
    if effects is not None:
        grouped = {}
        for event in execution_events:
            if event["kind"] == "write":
                key = (event["position"], event["path"])
                if key in grouped:
                    grouped[key]["contents"] += tuple(event["contents"])
                else:
                    grouped[key] = dict(event, contents=tuple(event["contents"]))
        ordered = sorted([*grouped.values(), *(event for event in execution_events if event["kind"] == "read")],
                         key=lambda event: (event["position"], event["kind"] != "write"))
        effects.extend(shell_effect_order(tokens, ordered))
    for identifier, language in languages.items():
        if language:
            variants = variants_of(identifier)
            if variants is not None:
                variants = (variants,) if isinstance(variants, UnresolvedStream) else variants
                owns |= any(stdin_ownership(content, language) for content in variants)
    if owns:
        raise ExecutableOwnership("executable stdin")
    # Here-string words are stdin data. Remove them from later argv inspection.
    masked, cursor = [], 0
    for index, token in enumerate(tokens):
        start = code.find(token, cursor)
        if start < 0:
            continue
        cursor = start + len(token)
        if index - 1 in inline_inputs:
            masked.append((start, cursor))
    for start, end in reversed(masked):
        code = code[:start] + "'inert-stdin-body'" + code[end:]
    return code


def shell_effect_order(tokens, events):
    """Replay repeated bodies and defer function effects until a real call.

    Two iterations suffice for literal file writes: the second read observes
    the first iteration's final replacement, rather than any intermediate write.
    Straight-line effects keep their original command order.
    """
    bounds, definitions, by_position = compound_bounds(tokens), {}, {}
    guards = [True] * len(tokens)

    def status(words):
        words = [word for word in words if word not in {";", "\n"}]
        return True if words in (["true"], [":"]) else False if words == ["false"] else None

    def guard(begin, end, condition):
        for at in range(begin, end):
            if condition is False or guards[at] is False:
                guards[at] = False
            elif condition is None or guards[at] is None:
                guards[at] = None

    # A skipped or conditional replacement cannot erase an earlier writer.
    # Literal true/false commands still permit their definite supported path.
    begin, executes, previous = 0, True, None
    for end in range(len(tokens) + 1):
        separator = tokens[end] if end < len(tokens) else ";"
        if separator not in {";", "\n", "&&", "||", "&", "|", "|&"}:
            continue
        guard(begin, end, executes)
        result = status(tokens[begin:end])
        if executes is True:
            previous = result
        elif executes is None and previous != result:
            previous = None
        if separator in {"&&", "||"}:
            executes = previous if separator == "&&" or previous is None else not previous
        else:
            executes, previous = True, None
        begin = end + 1
    for start, stop in bounds.items():
        if tokens[start] != "if":
            continue
        branch, other, cursor = None, None, start + 1
        while cursor < stop:
            if tokens[cursor] == "then" and branch is None:
                branch = cursor
            elif tokens[cursor] in {"else", "elif"}:
                other = cursor
                break
            cursor = bounds.get(cursor, cursor) + 1
        if branch is not None:
            condition = status(tokens[start + 1:branch])
            guard(branch, other if other is not None else stop, condition)
            if other is not None:
                inverse = not condition if condition is not None and tokens[other] == "else" else None
                guard(other, stop, inverse)
    for index, token in enumerate(tokens):
        if token != "{" or index not in bounds:
            continue
        if index >= 3 and tokens[index - 2:index] == ["(", ")"] and re.fullmatch(r"[A-Za-z_]\w*", tokens[index - 3]):
            definitions[index - 3] = (tokens[index - 3], index + 1, bounds[index])
        elif index >= 2 and tokens[index - 2] == "function" and re.fullmatch(r"[A-Za-z_]\w*", tokens[index - 1]):
            definitions[index - 2] = (tokens[index - 1], index + 1, bounds[index])
    for event in events:
        condition = guards[event["position"]]
        if condition is not False:
            if condition is None and event["kind"] == "write":
                event = dict(event, replaces=False)
            by_position.setdefault(event["position"], []).append(event)
    functions = {}

    def walk(begin, end, active=(), in_loop=False):
        index = begin
        while index < end:
            if index in definitions:
                name, start, stop = definitions[index]
                functions[name] = (start, stop)
                index = stop + 1
                continue
            token = tokens[index]
            if token in {"for", "select", "while", "until"} and index in bounds:
                stop = bounds[index]
                header_end = next((at for at in range(index + 1, stop) if tokens[at] in {";", "\n"}), stop)
                header = tokens[index + 1:header_end]
                iterations = 2
                if token == "for" and len(header) >= 2 and header[1] == "in":
                    items = header[2:]
                    if not any(re.search(r"[$`*?{]", word) for word in items):
                        iterations = min(2, len(items))
                if token == "while" and header == ["false"] or token == "until" and header == ["true"]:
                    iterations = 0
                for _ in range(iterations):
                    transfer = None
                    for event in walk(index + 1, stop, active, in_loop=True):
                        if event["kind"] in {"break", "continue"}:
                            transfer = event["kind"]
                            break
                        yield event
                    if transfer == "break":
                        break
                index = stop + 1
                continue
            current = by_position.get(index, ())
            if (in_loop and token in {"break", "continue"}
                    and any(event["kind"] == "read" and event["paths"] == [token] for event in current)
                    and not any(tokens[start] in {"if", "case"} and start < index < stop
                                for start, stop in bounds.items())):
                yield {"kind": token}
                return
            called = next((path for event in current if event["kind"] == "read"
                           for path in event["paths"] if path in functions), None)
            if called is not None:
                if active.count(called) < 2:
                    yield from walk(*functions[called], (*active, called))
            else:
                yield from current
            index += 1
    return list(walk(0, len(tokens)))


def unparsed_command_text(value):
    """Keep command expressions and referenced declarations, excluding JS prose.

    This is conservative command discovery, not evaluation or completion proof.
    Parsed literal tool arguments have already been removed by the caller.
    """
    atoms = list(re.finditer(r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`''', value))
    code = list(value)
    literals = []
    for atom in atoms:
        if not atom[0].startswith(("//", "/*")):
            literals.append(atom)
        code[atom.start():atom.end()] = ["\n" if c == "\n" else " " for c in atom[0]]
    code = "".join(code)
    def region(start, delimiter):
        start += len(re.match(r"\s*", value[start:])[0])
        end = re.search(delimiter, code[start:])
        return start, start + end.start() if end else len(code)
    declarations = {m[1]: region(m.end(), r"[;\n]")
                    for m in re.finditer(r"\b(?:const|let|var)\s+(\w+)\s*=", code)}
    pending = [region(m.end(), r"[,};\n]") for m in re.finditer(r"\b(?:cmd|command)\s*:", code)]
    pending += [(m.start(1), m.end(1)) for m in re.finditer(r"[{,]\s*(cmd|command)\s*(?=[,}])", code)]
    seen, pieces, unresolved = set(), [], False
    def literal_writers():
        for literal in literals:
            try:
                text = ast.literal_eval(literal[0]) if not literal[0].startswith('`') else literal[0][1:-1]
            except (ValueError, SyntaxError):
                continue
            if isinstance(text, str) and executable_content(text):
                yield text
    writers = list(literal_writers())
    while pending:
        start, end = pending.pop()
        if (start, end) in seen:
            continue
        seen.add((start, end))
        expression = value[start:end].strip()
        if expression.startswith('`'):
            command = re.sub(r"\$\{[^}]*\}", "computed-operand", expression.strip('`'))
            substitutions = re.findall(r"\$\{([^}]*)\}", expression)
            safe_substitutions = True
            for substitution in substitutions:
                bound = declarations.get(substitution.strip())
                try:
                    operand = ast.literal_eval(value[bound[0]:bound[1]].strip()) if bound else None
                except (ValueError, SyntaxError):
                    operand = None
                if not isinstance(operand, (str, int, float)) or any(c in str(operand) for c in "`$;&|\n\"'"):
                    safe_substitutions = False
            if (safe_substitutions or not writers) and not any(c in command for c in "`$;&|\n"):
                # Interpolation may introduce shell syntax. Known safe operands
                # or the absence of executable ownership literals preserve reads.
                argv = shlex.split(command)
                if argv and Path(argv[0]).name in {"rg", "grep", "cat", "sed", "head", "tail", "ls"}:
                    continue
                helper, action = helper_action(argv)
                if (helper in {"gh-plan.py", "gh-pr.py"}
                        and action[:1] in (["search"], ["show"], ["index"], ["view"], ["list"])):
                    continue
        fragment = code[start:end] + " " + " ".join(m[0] for m in literals if start <= m.start() < end)
        pieces.append(fragment)
        words = re.findall(r"\b\w+\b", code[start:end])
        words += re.findall(r"\$\{\s*(\w+)\s*\}", expression)
        pending.extend(declarations[word] for word in words if word in declarations)
        unresolved |= any(word not in declarations for word in words)
    if unresolved:
        # Parameters, destructuring and for-of bindings do not have a simple
        # declaration to follow. They must not hide executable cell literals.
        pieces.extend(writers)
    return " ".join(pieces)


def opaque_ownership(name, value):
    """Unparsed potentially writing code is uncertainty, never proof of no write."""
    value = arguments(value)
    for text in shell_texts(name, value):
        try:
            code = shell_code(text)
        except ExecutableOwnership:
            return True
        except ValueError:
            if OWNERSHIP_TEXT.search(text):
                return True
            code = ""
        for expansion in shell_expansions(code):
            if executable_content(expansion) or opaque_ownership("exec_command", {"cmd": expansion}):
                return True
        for argv in shell_commands("exec_command", {"cmd": text}):
            child = unwrapped_command(argv)
            executable = helper_argv(argv)
            helper_name, action = helper_action(argv)
            if (helper_name in {"gh-plan.py", "gh-pr.py"} and action
                    and any(symbol in action[0] for symbol in "$`")):
                return True
            if (executable and executable[0].startswith("$")
                    and (action[:1] in (["claim"], ["create"]) or action[:2] == ["pr", "create"])):
                return True
            if not ownership_command(argv):
                for command in executor_commands(child):
                    if executable_content(command) or opaque_ownership("exec_command", {"cmd": command}):
                        return True
            if child and Path(child[0]).name in {"bash", "sh", "zsh", "dash", "ksh"}:
                body = shell_c_body(child)
                if body is not None and (executable_content(body) or opaque_ownership("exec_command", {"cmd": body})):
                    return True
            if child and re.fullmatch(r"python[0-9.]*", Path(child[0]).name):
                code = child[child.index("-c") + 1:] if "-c" in child else []
                if any(python_ownership(body) for body in code):
                    return True
            if child and Path(child[0]).name == "xargs" and argument_execution_language(child) and OWNERSHIP_TEXT.search(" ".join(child[1:])):
                return True
            if child and Path(child[0]).name in {"eval", "node"} and OWNERSHIP_TEXT.search(" ".join(child[1:])):
                return True
    if name in {"exec", "functions.exec"} and isinstance(value, str):
        unparsed = unparsed_command_text(SHELL_FIELD.sub("", value))
        return bool(re.search(r"gh-plan\.py", unparsed) and re.search(r"\bclaim\b", unparsed)
                    or re.search(r"gh-pr\.py|\bgh\s+pr\b", unparsed) and re.search(r"\bcreate\b", unparsed))
    return False


def executor_commands(argv):
    """Inspect only operands that a process launcher actually executes.

    Body/message/config arguments are data, even when they contain complete
    ownership commands. Unknown executable variables remain uncertain.
    """
    if not argv:
        return
    name, cursor = Path(argv[0]).name, 1
    if name == "uv" and len(argv) > 1 and not argv[1].startswith("-") and argv[1] != "run":
        return
    if name.startswith("$") or name == "uv":
        # An unresolved interpreter or uv launcher can still expose a literal
        # helper argv. Do not interpret arbitrary multiword argument text.
        for index, word in enumerate(argv[1:], 1):
            if Path(word).name in {"gh-plan.py", "gh-pr.py", "gh"}:
                yield shlex.join(argv[index:])
        if name.startswith("$"):
            for index, word in enumerate(argv[:-1]):
                if word in {"-c", "--command"}:
                    yield argv[index + 1]
        return
    if name in {"bash", "sh", "zsh", "dash", "ksh"}:
        # --emulate is a zsh value option not accepted by the foreground
        # normalizer. Its -c operand still contains executable shell code.
        if name == "zsh" and argv[1:2] == ["--emulate"] and len(argv) > 2:
            body = shell_c_body([argv[0], *argv[3:]])
            if body is not None:
                yield body
        return
    value_options = {
        "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from", "-R", "--chroot", "-D", "--chdir", "-r", "--role", "-t", "--type"},
        "doas": {"-u", "-C"},
        "flock": {"-E", "--conflict-exit-code", "-w", "--timeout"},
        "su": {"-c", "--command", "--session-command", "-s", "--shell", "-g", "--group", "-G", "--supp-group", "-w", "--whitelist-environment"},
        "runuser": {"-c", "--command", "--session-command", "-u", "--user", "-s", "--shell", "-g", "--group", "-G", "--supp-group", "-w", "--whitelist-environment"},
        "ssh": {"-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w"},
    }.get(name)
    if value_options is None:
        return
    user_seen, direct_user = False, False
    command_options = {"-c", "--command"}
    if name in {"su", "runuser"}:
        command_options.add("--session-command")
    while cursor < len(argv):
        option, equal, _ = argv[cursor].partition("=")
        if name in {"flock", "su", "runuser"} and option in command_options:
            if equal:
                yield argv[cursor].partition("=")[2]
            elif cursor + 1 < len(argv):
                yield argv[cursor + 1]
            return
        if option == "--":
            cursor += 1
            break
        if name in {"su", "runuser"} and option == "-":
            cursor += 1  # Login shell marker is not a command or a user.
            continue
        if not option.startswith("-"):
            if name in {"su", "runuser"} and not user_seen and not direct_user:
                user_seen = True
                cursor += 1
                continue  # su permits its own options after the user operand.
            break
        if name == "runuser" and option in {"-u", "--user"}:
            direct_user = True
        cursor += 1
        takes_next = option in value_options
        if not option.startswith("--") and not takes_next:
            cluster = option[1:]
            for index, letter in enumerate(cluster):
                if "-" + letter in value_options:
                    if name in {"su", "runuser"} and letter == "c":
                        if cluster[index + 1:]:
                            yield cluster[index + 1:]
                        elif cursor < len(argv):
                            yield argv[cursor]
                        return
                    if name == "runuser" and letter == "u":
                        direct_user = True
                    takes_next = index == len(cluster) - 1
                    break
        if takes_next and not equal:
            cursor += 1
    if name in {"ssh", "flock"}:
        cursor += 1  # Remote host or lock file precedes the command.
    if cursor < len(argv):
        remaining = argv[cursor:]
        option, equal, inline = remaining[0].partition("=")
        if name == "flock" and option in {"-c", "--command"}:
            if equal:
                yield inline
            elif len(remaining) > 1:
                yield remaining[1]
        elif name not in {"su", "runuser"} or name == "runuser" and direct_user:
            yield " ".join(remaining) if name == "ssh" else shlex.join(remaining)


def shell_expansions(text, *, heredoc=False):
    """Read substitutions; unquoted heredoc body quotes do not suppress them."""
    quote, escaped, cursor = None, False, 0
    while cursor < len(text):
        char = text[cursor]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif (text[cursor:cursor + 2] == "$(" and quote != "'"
              or not heredoc and quote is None and text[cursor:cursor + 2] in {"<(", ">("}):
            start, end, depth = cursor + 2, cursor + 2, 1
            inner_quote, inner_escape = None, False
            while end < len(text) and depth:
                inner = text[end]
                if inner_escape:
                    inner_escape = False
                elif inner == "\\" and inner_quote != "'":
                    inner_escape = True
                elif inner_quote:
                    if inner == inner_quote:
                        inner_quote = None
                elif inner in "'\"":
                    inner_quote = inner
                elif inner in "()":
                    depth += 1 if inner == "(" else -1
                end += 1
            yield text[start:end - 1] if depth == 0 else text[start:]
            cursor = end
            continue
        elif heredoc and char == "`":
            start, end, inner_escape = cursor + 1, cursor + 1, False
            while end < len(text):
                if inner_escape:
                    inner_escape = False
                elif text[end] == "\\":
                    inner_escape = True
                elif text[end] == "`":
                    break
                end += 1
            yield text[start:end]
            cursor = end + 1
            continue
        elif quote:
            if char == quote:
                quote = None
        elif not heredoc and char in "'\"":
            quote = char
        cursor += 1


def unwrapped_command(argv, *, unwrap_code=True):
    """Transparent process wrappers change execution, never operand meaning."""
    argv = list(argv)
    while argv:
        name = Path(argv[0]).name
        if re.match(r"[A-Za-z_]\w*=", argv[0]) or name in {"if", "elif", "while", "until", "!", "{", "(", "do", "then", "else", "builtin"}:
            argv = argv[1:]
        elif name in {"command", "exec", "nohup", "time", "env", "timeout", "nice", "stdbuf"}:
            argv = argv[1:]
            value_options = {
                "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
                "timeout": {"-k", "--kill-after", "-s", "--signal"},
                "nice": {"-n", "--adjustment"},
                "time": {"-o", "--output"},
                "stdbuf": {"-i", "--input", "-o", "--output", "-e", "--error"},
                "exec": {"-a"},
            }.get(name, set())
            while argv and (argv[0].startswith("-") or re.match(r"[A-Za-z_]\w*=", argv[0])):
                flag = argv.pop(0)
                option, equal, inline = flag.partition("=")
                if option in value_options and (equal or argv):
                    operand = inline if equal else argv.pop(0)
                    if name == "env" and option in {"-S", "--split-string"}:
                        try:
                            argv = shlex.split(operand) + argv
                        except ValueError:
                            return ["env", flag, operand, *argv]
                        break
            if name == "timeout" and argv:
                argv = argv[1:]
        elif name == "uv":
            child = uv_run_argv(argv)
            if child is None:
                break
            # uv's '-' is Python stdin even behind env/assignment wrappers.
            # Preserve that execution mode while normalizing the launcher.
            argv = ["python", *child] if child[:1] == ["-"] else child
        elif unwrap_code and (((body := shell_c_body(argv)) is not None and (child := foreground_shell_argv(body)))
              or (re.fullmatch(r"python[0-9.]*", name) and len(argv) == 3 and argv[1] == "-c"
                  and (child := foreground_python_argv(argv[2])))):
            argv = child
        else:
            break
    return argv


def runtime_uv_help():
    """Probe the trusted runtime once; never execute a transcript's uv path."""
    executable = shutil.which("uv")
    if executable is None:
        return None
    try:
        result = subprocess.run([executable, "run", "--help"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


@lru_cache(maxsize=1)
def uv_run_options():
    """Read current uv option arity from its own help rather than a copied table."""
    help_text = runtime_uv_help()
    if not isinstance(help_text, str):
        return None
    options = {}
    for line in help_text.splitlines():
        if not line.lstrip().startswith("-"):
            continue
        signature = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
        takes_value = re.search(r"<[^>]+>", signature) is not None
        for option in re.findall(r"(?<![\w-])--?[A-Za-z][\w-]*", signature):
            options[option] = takes_value
    if "--help" not in options:
        return None
    # uv still accepts these deprecated aliases but omits them from help.
    # Preserve their verified arity without freezing the visible option set.
    for option, takes_value in HIDDEN_UV_RUN_OPTIONS.items():
        options.setdefault(option, takes_value)
    return options


def uv_run_argv(argv):
    """Normalize only options supported by the trusted current uv runtime."""
    words = list(argv[1:])
    def consume(words):
        while words and words[0].startswith("-"):
            if words[0] == "-":
                return words
            if words[0] == "--":
                return words[1:]
            options = uv_run_options()
            if options is None:
                return None
            flag = words.pop(0)
            option, equal, _ = flag.partition("=")
            if option in options:
                takes_value = options[option] and not equal
            elif flag.startswith("-") and not flag.startswith("--"):
                takes_value = False
                for position, character in enumerate(flag[1:], 2):
                    short = "-" + character
                    if short not in options:
                        return None
                    if options[short]:
                        takes_value = position == len(flag)
                        break
            else:
                return None
            if takes_value:
                if not words:
                    return None
                words.pop(0)
        return words
    words = consume(words)
    if words is None or words[:1] != ["run"]:
        return None
    return consume(words[1:])


def shell_c_body(argv):
    """Find the command argument after shell option clusters and value options."""
    if not argv or Path(argv[0]).name not in {"bash", "sh", "zsh", "dash", "ksh"}:
        return None
    cursor = 1
    while cursor < len(argv):
        option = argv[cursor]
        if option == "--" or not option.startswith(("-", "+")):
            return None
        if not option.startswith("--") and "c" in option[1:]:
            return argv[cursor + 1] if cursor + 1 < len(argv) else None
        cursor += 1
        if option in {"--rcfile", "--init-file"} or not option.startswith("--") and option.endswith(("o", "O")):
            cursor += 1
    return None


def foreground_shell_text(content):
    """Literal shell control flow and pipes wait; detached/computed code does not."""
    if "$(" in content or "`" in content:
        return False
    try:
        lexer = shell_lexer(shell_code(content), posix=False)
        lexer.whitespace_split = True
        return "&" not in list(lexer)
    except ValueError:
        return False


def foreground_shell_argv(content):
    """Literal foreground commands wait; cd and output redirection do not detach."""
    if "$(" in content or "`" in content:
        return None
    try:
        lexer = shell_lexer(content)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        argv = list(lexer)
    except ValueError:
        return None
    if len(argv) >= 3 and argv[0] == "cd" and argv[2] == "&&":
        argv = argv[3:]
    return argv if argv and not any(all(c in ";&|\n" for c in token) for token in argv) else None


def foreground_python_argv(content):
    """Recognize only one literal blocking subprocess call, with no other code."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    statements = [node for node in tree.body if not (
        isinstance(node, ast.Import) and len(node.names) == 1
        and node.names[0].name == "subprocess" and node.names[0].asname is None)]
    if len(statements) != 1 or not isinstance(statements[0], ast.Expr):
        return None
    call = statements[0].value
    if (not isinstance(call, ast.Call) or len(call.args) != 1
            or ast.unparse(call.func) not in {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}):
        return None
    try:
        argv = ast.literal_eval(call.args[0])
        options = {keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords}
    except (ValueError, TypeError):
        return None
    if (None in options or "executable" in options or options.get("shell", False) is not False
            or not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(word, str) for word in argv)):
        return None
    return list(argv)


def python_ownership(content):
    """Follow actual Python launch operands through their reaching bindings."""
    try:
        tree = ast.parse(textwrap.dedent(content))
    except SyntaxError:
        return False
    launches = {"os.system", "os.popen", "subprocess.run", "subprocess.Popen", "subprocess.call",
                "subprocess.check_call", "subprocess.check_output", "exec", "eval"}
    found = False

    def value(node, bindings):
        if isinstance(node, ast.Name):
            return bindings.get(node.id, (None, ()))
        if isinstance(node, ast.Constant):
            return node.value, (node.value,) if isinstance(node.value, str) else ()
        if isinstance(node, (ast.List, ast.Tuple)):
            children = [value(element, bindings) for element in node.elts]
            # Unknown arguments do not hide a known executable/action, nor make
            # an echo/rg argv execute the ownership-shaped data it contains.
            return ([item if isinstance(item, str) else "$computed_operand" for item, _ in children],
                    tuple(piece for _, evidence in children for piece in evidence))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, left_pieces = value(node.left, bindings)
            right, right_pieces = value(node.right, bindings)
            literal = left + right if isinstance(left, (str, list, tuple)) and type(left) is type(right) else None
            return literal, left_pieces + right_pieces
        if (isinstance(node, ast.Call) and ast.unparse(node.func) == "shlex.split"
                and len(node.args) == 1 and not node.keywords):
            text, pieces = value(node.args[0], bindings)
            if isinstance(text, str):
                try:
                    return shlex.split(text), pieces
                except ValueError:
                    pass
            return None, pieces
        return None, tuple(piece for child in ast.iter_child_nodes(node)
                           for piece in value(child, bindings)[1])

    def scan(node, bindings):
        nonlocal found
        function = ast.unparse(node.func) if isinstance(node, ast.Call) else None
        operand = node.args[0] if isinstance(node, ast.Call) and node.args else None
        if isinstance(node, ast.Call) and function in launches and operand is None:
            parameter = "args" if function.startswith("subprocess.") else "cmd" if function == "os.popen" else "command"
            operand = next((keyword.value for keyword in node.keywords if keyword.arg == parameter), None)
        if function in launches and operand is not None:
            command, pieces = value(operand, bindings)
            if isinstance(command, (list, tuple)):
                argv = unwrapped_command(command)
                found |= ownership_command(argv)
                found |= any(executable_content(body) for body in executor_commands(argv))
                if "$computed_operand" in command:
                    found |= opaque_ownership("exec_command", {"cmd": shlex.join(command)})
                    if argv and (argv[0].startswith("$") or executable_wrapper(argv)):
                        found |= bool(OWNERSHIP_TEXT.search(" ".join(pieces))
                                      or OWNERSHIP_TEXT.search("".join(pieces)))
            elif isinstance(command, str):
                found |= executable_content(command)
            else:
                found |= bool(OWNERSHIP_TEXT.search(" ".join(pieces))
                              or OWNERSHIP_TEXT.search("".join(pieces)))
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.stmt):
                scan(child, bindings)

    def merge(*states):
        merged = {}
        for name in set().union(*(state.keys() for state in states)):
            alternatives = [state.get(name, (None, ())) for state in states]
            literal = alternatives[0][0]
            if any(other[0] != literal for other in alternatives[1:]):
                literal = None
            merged[name] = literal, tuple(dict.fromkeys(piece for _, pieces in alternatives for piece in pieces))
        return merged

    def walk(statements, bindings):
        for statement in statements:
            scan(statement, bindings)
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                assigned = value(statement.value, bindings) if statement.value is not None else (None, ())
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        if isinstance(statement, ast.AugAssign):
                            prior, pieces = bindings.get(target.id, (None, ()))
                            literal, additional = assigned
                            literal = prior + literal if isinstance(statement.op, ast.Add) and isinstance(prior, (str, list, tuple)) and type(prior) is type(literal) else None
                            assigned = literal, pieces + additional
                        bindings[target.id] = assigned
            elif isinstance(statement, ast.If):
                branches = (statement.body, statement.orelse)
                if isinstance(statement.test, ast.Constant):
                    branches = (branches[0] if statement.test.value else branches[1],)
                bindings = merge(*(walk(branch, dict(bindings)) for branch in branches))
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                repeated = merge(bindings, walk(statement.body, dict(bindings)))
                # A subsequent iteration can execute with a binding written at
                # the end of the previous one.
                repeated = merge(repeated, walk(statement.body, dict(repeated)))
                bindings = walk(statement.orelse, repeated)
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                normal = walk(statement.body, dict(bindings))
                exceptional = merge(bindings, normal)
                alternatives = [walk(statement.orelse, normal)]
                alternatives.extend(walk(handler.body, dict(exceptional)) for handler in statement.handlers)
                bindings = walk(statement.finalbody, merge(*alternatives))
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                bindings = walk(statement.body, bindings)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local = dict(bindings)
                if not isinstance(statement, ast.ClassDef):
                    for argument in (*statement.args.posonlyargs, *statement.args.args, *statement.args.kwonlyargs):
                        local.pop(argument.arg, None)
                walk(statement.body, local)
            elif isinstance(statement, ast.Match):
                bindings = merge(bindings, *(walk(case.body, dict(bindings)) for case in statement.cases))
        return bindings

    walk(tree.body, {})
    return found


def python_fragment_ownership(content):
    """Partial Python edits retain Python string boundaries, never shell syntax."""
    try:
        ast.parse(textwrap.dedent(content))
    except SyntaxError:
        pass
    else:
        return python_ownership(content)
    # A patch hunk need not be a complete Python module. Tokenization preserves
    # quoted fixture data even without its surrounding block's indentation.
    tokens = []
    try:
        for token in tokenize.generate_tokens(io.StringIO("\n".join(line.lstrip() for line in content.splitlines())).readline):
            if token.type not in {tokenize.INDENT, tokenize.DEDENT, tokenize.NL, tokenize.COMMENT}:
                tokens.append((token.type, token.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass  # Retain complete tokens before an unfinished call or quote.
    for start, (kind, word) in enumerate(tokens):
        if kind != tokenize.NAME:
            continue
        cursor, function = start + 1, word
        while cursor + 1 < len(tokens) and tokens[cursor][1] == "." and tokens[cursor + 1][0] == tokenize.NAME:
            function += "." + tokens[cursor + 1][1]
            cursor += 2
        if (function not in {"os.system", "os.popen", "subprocess.run", "subprocess.Popen", "subprocess.call",
                             "subprocess.check_call", "subprocess.check_output", "exec", "eval"}
                or cursor == len(tokens) or tokens[cursor][1] != "("):
            continue
        depth, end = 0, cursor
        while end < len(tokens):
            spelling = tokens[end][1]
            if spelling in {"(", "[", "{"}:
                depth += 1
            elif spelling in {")", "]", "}"}:
                depth -= 1
            end += 1
            if depth == 0:
                break
        expression = tokenize.untokenize(tokens[start:end])
        if depth == 0:
            if python_ownership(expression):
                return True
            try:
                call = ast.parse(expression).body[0].value
                argument = call.args[0]
            except (ValueError, SyntaxError, AttributeError, IndexError):
                continue
            if isinstance(argument, ast.Name):
                for index in range(len(tokens) - 2):
                    if tokens[index] == (tokenize.NAME, argument.id) and tokens[index + 1][1] == "=":
                        end_definition, balance = index + 2, 0
                        while end_definition < len(tokens):
                            if balance == 0 and (tokens[end_definition][0] == tokenize.NEWLINE
                                                 or tokens[end_definition][1] == ";"):
                                break
                            spelling = tokens[end_definition][1]
                            if spelling in {"(", "[", "{"}:
                                balance += 1
                            elif spelling in {")", "]", "}"}:
                                balance -= 1
                            end_definition += 1
                        definition = tokenize.untokenize(tokens[index:end_definition])
                        if python_ownership(definition + "\n" + expression):
                            return True
            continue
        # Only a truly unfinished actual launch retains uncertain argv text.
        # Closed calls resolve their own argument instead of borrowing fixtures.
        literals = []
        for token_kind, literal in tokens[cursor:end]:
            if token_kind == tokenize.STRING:
                try:
                    value = ast.literal_eval(literal)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(value, str):
                    literals.append(value)
        if OWNERSHIP_TEXT.search(" ".join(literals)):
            return True
    return False


def executable_file_content(path, content):
    if isinstance(content, UnresolvedStream):
        return content.possible_ownership
    return python_fragment_ownership(content) if Path(path).suffix == ".py" else executable_content(content)


def executable_content(content):
    """Recognize executable ownership syntax, excluding quoted examples."""
    try:
        ast.parse(content)
    except SyntaxError:
        pass
    else:
        return python_ownership(content)
    try:
        shell_code(content)
    except ExecutableOwnership:
        return True
    except ValueError:
        pass  # Unknown syntax still follows the existing command classifier.
    return any(ownership_command(unwrapped_command(argv))
               for argv in shell_commands("exec_command", {"cmd": content}))


def executable_wrapper(argv):
    if not argv or script(argv) in {"gh-plan.py", "gh-pr.py"}:
        return False
    name = Path(argv[0]).name
    return (name in {"bash", "sh", "zsh", "dash", "ksh", "node", "uv", "env", "timeout", "eval", "xargs", "exec", "command", "nice", "time", "gh-plan.py", "gh-pr.py"}
            or re.fullmatch(r"python[0-9.]*", name) is not None
            or re.match(r"[A-Za-z_]\w*=", argv[0]) is not None)


def file_writes(name, value, *, replacements=None):
    """Read file provenance from native writes, never from current disk contents."""
    value = arguments(value)
    if name in {"Write", "Edit"} and isinstance(value, dict):
        if replacements is not None and name == "Write":
            replacements.add(value.get("file_path", ""))
        yield value.get("file_path", ""), value.get("content") or value.get("new_string") or ""
    patches = []
    if name in {"apply_patch", "functions.apply_patch"} and isinstance(value, str):
        patches.append(value)
    if name in {"exec", "functions.exec"} and isinstance(value, str):
        for match in re.finditer(r'''tools\.apply_patch\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')''', value):
            try:
                patches.append(ast.literal_eval(match[1]))
            except (SyntaxError, ValueError):
                pass
    for patch in patches:
        if replacements is not None:
            replacements.update(re.findall(r"(?m)^\*\*\* Add File: (.+)$", patch))
        for match in re.finditer(r"(?m)^\*\*\* (?:Add|Update) File: (.+)\n([\s\S]*?)(?=^\*\*\* |\Z)", patch):
            for hunk in re.split(r"(?m)^@@[^\n]*\n", match[2]):
                content = "\n".join(line[1:] for line in hunk.splitlines() if line.startswith(("+", " ")))
                if content:
                    yield match[1], content
    for text in shell_texts(name, value):
        writes = []
        try:
            shell_code(text, writes=writes, replacements=replacements)
        except ExecutableOwnership:
            yield from writes
            continue
        except ValueError:
            continue  # Existing opaque ownership handles unparsed shell text.
        yield from writes


def native_calls(records, harness, *, with_timestamp=False):
    cwd = None
    for record in records:
        payload = record.get("payload") or {}
        if record.get("type") == "session_meta":
            cwd = payload.get("cwd", cwd)
        cwd = record.get("cwd", cwd)
        for identifier, name, value in calls_and_results(record, harness)[0]:
            serial = composed_calls(value) if name in {"exec", "functions.exec"} else []
            if serial:
                for method, args, _ in serial:
                    call = (identifier, method, args, args.get("workdir") or args.get("cwd") or cwd)
                    yield (*call, record.get("timestamp")) if with_timestamp else call
                continue
            args = arguments(value)
            call = (identifier, name, value, (args.get("workdir") or args.get("cwd") or args.get("working_directory") or cwd) if isinstance(args, dict) else cwd)
            yield (*call, record.get("timestamp")) if with_timestamp else call


def native_path(path, cwd):
    if posixpath.isabs(path):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(cwd, path)) if cwd else None


def native_file_effects(name, value):
    """Keep native patch and shell effects in their visible call order."""
    native = arguments(value)
    if name in {"exec", "functions.exec"} and isinstance(native, str):
        patches = list(re.finditer(r'''tools\.apply_patch\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')''', native))
        if patches:
            calls = [(match.start(), "apply_patch", match[1]) for match in patches]
            calls.extend((match.start(), "exec_command", match[1]) for match in SHELL_FIELD.finditer(native))
            for _, method, raw in sorted(calls):
                try:
                    content = ast.literal_eval(raw)
                except (SyntaxError, ValueError):
                    continue
                yield from native_file_effects(method, {"cmd": content} if method == "exec_command" else content)
            return
    texts = list(shell_texts(name, value))
    if texts:
        for text in texts:
            effects = []
            try:
                shell_code(text, effects=effects)
            except ValueError:
                pass  # Typed ownership retains the effects collected first.
            yield from effects
        return
    replacements, grouped = set(), {}
    for path, content in file_writes(name, value, replacements=replacements):
        grouped.setdefault(path, []).append(content)
    for path, contents in grouped.items():
        yield {"kind": "write", "path": path, "contents": tuple(contents), "replaces": path in replacements}


def apply_file_effect(effect, cwd, candidates):
    if effect["kind"] != "write":
        return
    path = native_path(effect["path"], cwd) if effect["path"] else None
    if path:
        owning = any(executable_file_content(effect["path"], content) for content in effect["contents"])
        if owning:
            candidates.add(path)
        elif effect["replaces"]:
            candidates.discard(path)


def native_timestamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def file_write_completions(records, harness, identifiers):
    """Use the existing native binding for a replacement's completion bound."""
    selected = {(name, id(value)) for record in records
                for identifier, name, value in calls_and_results(record, harness)[0] if identifier in identifiers}
    complete = {}
    for index, source, value in bound_outputs(records, harness, lambda name, value: (name, id(value)) in selected):
        if value.get("_result") and not value.get("_pending"):
            at = native_timestamp(records[index].get("timestamp"))
            if at is not None:
                complete[source] = at
    return complete


def ownership_file_candidates(histories):
    """Preserve each peer write's native interval, not its eventual contents.

    First-record identity excludes a history's own future writes. A writer is
    possible from launch; only a completed replacement can erase that evidence.
    """
    candidates = {}
    for node in histories:
        origin = id(node["records"][0]) if node["records"] else None
        writes = []
        for identifier, name, value, cwd, stamp in native_calls(node["records"], node["harness"], with_timestamp=True):
            for effect in native_file_effects(name, value):
                if effect["kind"] == "write":
                    writes.append((identifier, cwd, native_timestamp(stamp), effect))
        by_call = {}
        for identifier, cwd, timestamp, effect in writes:
            path = native_path(effect["path"], cwd) if effect["path"] else None
            if path:
                entry = by_call.setdefault((identifier, path), {"at": timestamp, "owns": False, "final": None})
                owning = any(executable_file_content(effect["path"], content) for content in effect["contents"])
                entry["owns"] |= owning
                if owning or effect["replaces"]:
                    entry["final"] = owning
        replacements = {identifier for (identifier, _), entry in by_call.items()
                        if entry["final"] is False and entry["at"] is not None}
        completions = file_write_completions(node["records"], node["harness"], replacements) if replacements else {}
        for (identifier, path), entry in by_call.items():
            if entry["owns"]:
                candidates.setdefault(path, []).append((origin, entry["at"], True, False))
            if entry["final"] is False:
                at = completions.get(identifier, float("inf")) if entry["at"] is not None else None
                candidates.setdefault(path, []).append((origin, at, False, True))
    return candidates


def execution_targets(argv):
    child = unwrapped_command(argv)
    if not child:
        return []
    name = Path(child[0]).name
    interprets = child[0] == "." or name in {"bash", "sh", "zsh", "dash", "ksh", "source"} or re.fullmatch(r"python[0-9.]*", name)
    if interprets:
        if any(option == "-m" or option.startswith("-") and "c" in option for option in child[1:]):
            return []
        if "<" in child:
            return child[child.index("<") + 1:child.index("<") + 2]
        return next(([word] for word in child[1:] if not word.startswith("-")), [])
    return [child[0]]


def file_ownership_executions(records, harness, *, file_candidates=()):
    """Link actual script reads to the contents preceding them in native time."""
    origin = id(records[0]) if records else None
    candidates, peer_writes = set(), []
    if isinstance(file_candidates, dict):
        for path, events in file_candidates.items():
            for source, timestamp, owning, replaces in events:
                if source != origin and timestamp != float("inf"):
                    peer_writes.append((timestamp, path, owning, replaces, source))
    else:
        candidates.update(file_candidates)
    # Untimestamped historical fixtures retain their native sequence. Real
    # timestamps order all peers; an unknown reader sees their latest state.
    peer_writes.sort(key=lambda event: float("-inf") if event[0] is None else event[0])
    groups = []
    for at, *event in peer_writes:
        if not groups or groups[-1][0] != at:
            groups.append((at, []))
        groups[-1][1].append(event)
    next_peer = 0
    invocations = set()
    for identifier, name, value, cwd, stamp in native_calls(records, harness, with_timestamp=True):
        timestamp = native_timestamp(stamp)
        while next_peer < len(groups):
            at, events = groups[next_peer]
            if at is not None and timestamp is not None and at > timestamp:
                break
            if at is not None and at == timestamp:
                # Native timestamp resolution cannot order a simultaneous peer
                # completion. Keep every possible writer, and reconsider the
                # whole group at the next strictly later call.
                candidates.update(path for path, owning, _, _ in events if owning)
                break
            final = {}
            for path, owning, replaces, source in events:
                if owning or replaces:
                    final.setdefault(path, {})[source] = owning
            for path, origins in final.items():
                if any(origins.values()):
                    candidates.add(path)
                else:
                    candidates.discard(path)
            next_peer += 1
        for effect in native_file_effects(name, value):
            if effect["kind"] == "write":
                apply_file_effect(effect, cwd, candidates)
            elif {native_path(path, cwd) for path in effect["paths"]} & candidates:
                invocations.add(identifier)
    return invocations


class GroupShellLexer(shlex.shlex):
    """Preserve raw shell words and split adjacent structural punctuation."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._word_cursor = 0

    def read_token(self):
        if self.posix:
            return super().read_token()
        # Let shlex's POSIX quote state handle quotes inside an option word,
        # while retaining the raw source slice needed for descriptor adjacency.
        source = self.instream.getvalue()
        start = self.instream.tell() - len(self._pushback_chars)
        while start < len(source) and source[start] in self.whitespace:
            start += 1
        self.posix = True
        try:
            super().read_token()
        finally:
            self.posix = False
        end = self.instream.tell() - len(self._pushback_chars)
        if end > start and source[end - 1] in self.whitespace:
            end -= 1
        return source[start:end] if end > start else self.eof

    def get_token(self):
        token = super().get_token()
        if self.posix or token == self.eof:
            return token
        source = self.instream.getvalue()
        start = source.find(token, self._word_cursor)
        end = start + len(token) if start >= 0 else self._word_cursor
        punctuation = token and all(char in self.punctuation_chars for char in token)
        if punctuation and any(char in token for char in "()"):
            parts = re.findall(r"[()]|[^()]+", token)
            self.pushback.extendleft(reversed(parts[1:]))
            self._word_cursor = start + len(parts[0]) if start >= 0 else end
            return parts[0]
        if not punctuation and start >= 0:
            while True:
                following = super().get_token()
                if following == self.eof:
                    break
                next_start = source.find(following, end)
                if next_start != end or all(char in self.punctuation_chars for char in following):
                    self.push_token(following)
                    break
                token += following
                end += len(following)
        self._word_cursor = end
        return token


def shell_lexer(text, *, posix=True, punctuation_chars=";&|<>()\n"):
    """Preserve word-internal hashes; only unquoted word-start # is a comment."""
    cleaned, quote, escaped, boundary, comment = [], None, False, True, False
    for char in text:
        if comment:
            if char != "\n":
                continue
            comment = False
        if escaped:
            escaped = False
            if char == "\n":
                cleaned.pop()  # Shell continuation is removed before tokenization.
                continue
            boundary = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote, boundary = char, False
        elif char == "#" and boundary:
            comment = True
            continue
        else:
            boundary = char.isspace() or char in ";&|<>()"
        cleaned.append(char)
    lexer = GroupShellLexer("".join(cleaned), posix=posix, punctuation_chars=punctuation_chars)
    lexer.commenters = ""
    return lexer


def script(argv):
    argv = helper_argv(argv)
    if not argv:
        return ""
    return Path(argv[0]).name


def helper_argv(argv):
    """The helper and its arguments after transparent foreground launchers."""
    argv = unwrapped_command(argv)
    if argv and re.fullmatch(r"python[0-9.]*", Path(argv[0]).name):
        argv = argv[1:]
        while argv and argv[0] in {"-u", "-B", "-I", "-E", "-s", "-S"}:
            argv = argv[1:]
    return argv


def composed_calls(value):
    """Read only a complete sequence of visible, literal, serial native calls."""
    native = arguments(value)
    if not isinstance(native, str):
        return []
    native = re.sub(r"\A// @exec:[^\n]*\n", "", native)
    literal = r'''(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|-?\d+|true|false|null)'''
    key = r'''(?:\w+|"\w+"|'\w+')'''
    field = rf"{key}\s*:\s*{literal}"
    obj = rf"\{{\s*(?:{field}(?:\s*,\s*{field})*\s*,?)?\s*\}}"
    call = rf"text\(\s*(?P<paren>\()?\s*await\s+tools\.(?P<method>exec_command|write_stdin|wait)\(\s*(?P<args>{obj})\s*\)\s*(?(paren)\)\s*\.output)\s*\)\s*;?\s*"
    found, cursor = [], 0
    while cursor < len(native.rstrip()):
        match = re.compile(r"\s*" + call).match(native, cursor)
        if match is None:
            return []
        fields = {}
        for item in re.finditer(rf"({key})\s*:\s*({literal})", match["args"]):
            raw = item[2]
            try:
                fields[item[1].strip("\"'")] = {"true": True, "false": False, "null": None}.get(raw) if raw in {"true", "false", "null"} else ast.literal_eval(raw)
            except (SyntaxError, ValueError):
                return []
        found.append((match["method"], fields, bool(match["paren"])))
        cursor = match.end()
    return found


def single_composed_call(value, method):
    calls = composed_calls(value)
    return len(calls) == 1 and calls[0][0] == method and not calls[0][2]


def serial_native_chunk(record):
    """Read fresh top-level prints and the transport's own outer-cell status."""
    blocks = (record.get("payload") or {}).get("output")
    if not isinstance(blocks, list):
        return None, None
    if blocks and isinstance(blocks[0], dict):
        header = str(blocks[0].get("text", ""))
        running = re.match(r"Script running with cell ID ([\w-]+)\nWall time ", header)
        if running:
            return blocks[1:], running[1]
        if header.startswith("Script completed\nWall time "):
            return blocks[1:], None
    return blocks, None


def serial_native_results(blocks, serial):
    """Match printed calls to native blocks before interpreting their contents."""
    if blocks is None or len(blocks) != len(serial):
        return None
    wrappers = []
    for (_, _, stdout), block in zip(serial, blocks):
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            return None
        if stdout:
            wrappers.append(None)  # Preserve the slot, never decode read stdout.
            continue
        try:
            value = json.loads(block["text"])
        except ValueError:
            return None
        if (not isinstance(value, dict) or value.get("operation")
                or not (type(value.get("exit_code")) is int or type(value.get("session_id")) is int)):
            return None
        wrappers.append(value)
    return wrappers


def serial_ownership_records(records):
    """Bind visible serial calls, collecting only fresh prints across outer waits.

    Never split a computed/hidden call or borrow nested stdout as a native result.
    Checker receipts still use their separate isolated-invocation contract.
    """
    expanded, positions, launches, cells, polls, printed = [], [], {}, {}, {}, {}
    contexts = {}
    for index, record in enumerate(records):
        calls, results = calls_and_results(record, "codex")
        for identifier, name, value in calls:
            serial = composed_calls(value) if name in {"exec", "functions.exec"} else []
            if len(serial) > 1:
                launches[identifier] = serial
                contexts[identifier] = {key: record[key] for key in ("timestamp", "cwd") if key in record}
            args = arguments(value)
            if name in {"wait", "functions.wait"} and isinstance(args, dict):
                source = cells.get(str(args.get("cell_id")))
                if source is not None:
                    polls[identifier] = source
        split = None
        for identifier in results:
            source = identifier if identifier in launches else polls.get(identifier)
            if source is None:
                continue
            blocks, outer = serial_native_chunk(record)
            if blocks is None:
                continue
            printed.setdefault(source, []).extend(blocks)
            if outer:
                cells[outer] = source
                continue
            wrappers = serial_native_results(printed[source], launches[source])
            if wrappers is not None:
                split = []
                for part, ((method, args, _), wrapper) in enumerate(zip(launches[source], wrappers)):
                    part_id = f"{source}:serial:{part}"
                    split.append({**contexts[source], "payload": {"type": "function_call", "name": method, "call_id": part_id, "arguments": json.dumps(args)}})
                    # A stdout-only writer/poll has no native terminal wrapper.
                    # Retain its launch so it cannot become a false all-clear.
                    if wrapper is not None:
                        split.append({**({"timestamp": record["timestamp"]} if "timestamp" in record else {}),
                                      "payload": {"type": "function_call_output", "call_id": part_id, "output": json.dumps(wrapper)}})
        if split is not None:
            expanded.extend(split)
            positions.extend([index] * len(split))
        else:
            expanded.append(record)
            positions.append(index)
    # Remove only launches whose result was actually split. An incomplete cell
    # retains the original pending/opaque behavior.
    split_ids = {r["payload"]["call_id"].split(":serial:")[0] for r in expanded if ":serial:" in str(r.get("payload", {}).get("call_id", ""))}
    kept = [(r, p) for r, p in zip(expanded, positions)
            if not any(identifier in split_ids for identifier, _, _ in calls_and_results(r, "codex")[0])]
    return [r for r, _ in kept], [p for _, p in kept]


def isolated_command(name, value, helper):
    """One helper, optional leading cd, stderr merge and tee; no other output source."""
    texts = list(shell_texts(name, value))
    if len(texts) != 1 or "$(" in texts[0] or "`" in texts[0]:
        return False
    lexer = shell_lexer(texts[0].strip())
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    # Merging stderr preserves visible helper output; other redirections do not.
    normalized, cursor = [], 0
    while cursor < len(tokens):
        if tokens[cursor:cursor + 3] == ["2", ">&", "1"]:
            cursor += 3
        else:
            normalized.append(tokens[cursor])
            cursor += 1
    if len(normalized) >= 3 and normalized[0] == "cd" and normalized[2] == "&&":
        normalized = normalized[3:]
    operators = [t for t in normalized if t and all(c in ";&|<>\n" for c in t)]
    if not normalized or operators not in ([], ["|"]):
        return False
    pipe = normalized.index("|") if "|" in normalized else len(normalized)
    if script(normalized[:pipe]) != helper or (pipe < len(normalized) and normalized[pipe + 1:pipe + 2] != ["tee"]):
        return False
    native = arguments(value)
    if name in {"exec", "functions.exec"} and isinstance(native, str):
        return single_composed_call(value, "exec_command")
    return True


def ownership_activity(records, harness):
    """Missing/redirected ownership writes still require readback."""
    yield from ownership_state(records, harness)["indices"]


def ownership_state(records, harness, *, file_candidates=()):
    """Track launches through their terminal native result, including process polls."""
    positions = list(range(len(records)))
    if harness == "codex":
        records, positions = serial_ownership_records(records)
    no_writes = no_write_claims(records, harness)
    def owning(name, value):
        return opaque_ownership(name, value) or any(ownership_command(argv) for argv in shell_commands(name, value))
    indices, pending, events = [], set(), {}
    for index, source, value in bound_outputs(records, harness, owning, isolated_polls=True,
                                             opaque_calls=file_ownership_executions(records, harness, file_candidates=file_candidates)):
        if source in no_writes:
            continue
        events.setdefault(index, []).append((source, value))
    tasks, running = {}, {}
    for index, record in enumerate(records):
        for source, value in events.get(index, []):
            if value.get("_attempt"):
                pending.add(source)
                indices.append(index)
            elif value.get("_result"):
                indices.append(index)
                task = value.get("_background_task")
                if task:
                    tasks.setdefault(task, set()).add(source)
                if not value.get("_pending"):
                    pending.discard(source)
        if harness == "claude" and record.get("type") != "assistant":
            # Reuse the native terminal-notice reader. A TaskStop invocation
            # alone does not establish that a writer actually stopped.
            import session_record
            before = set(running)
            session_record.track_claude_background_tasks(record, running)
            for ended in before - running.keys():
                finished = tasks.pop(ended, set())
                for source in finished:
                    if not any(source in owners for owners in tasks.values()):
                        pending.discard(source)
                        indices.append(index)
    return {"indices": [positions[i] for i in indices], "pending": bool(pending)}


@lru_cache(maxsize=2)
def helper_parser(name):
    """Use the helpers' real argparse definitions without invoking any command."""
    path = Path(__file__).resolve().parents[2] / "github/scripts" / name
    module_name = "claim_receipts_" + name.replace("-", "_").removesuffix(".py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module.build_parser()


def parser_option(parser, token):
    try:
        parsed = parser._parse_optional(token)
    except Exception:
        return None
    # Python 3.14 returns candidate tuples; older argparse returns one tuple.
    if isinstance(parsed, list):
        parsed = parsed[0] if len(parsed) == 1 else None
    return (parsed[0], parsed[1], parsed[-1]) if parsed and parsed[0] is not None else None


def repository_option(token, helper="gh-plan.py"):
    parsed = parser_option(helper_parser(helper), token)
    return parsed is not None and parsed[0].dest == "repo"


def option_end(parser, words, index):
    """Consume one option with the authoritative argparse arity and prefixes."""
    parsed = parser_option(parser, words[index])
    if parsed is None:
        return None
    action, _, explicit = parsed
    if explicit is not None or action.nargs == 0:
        return index + 1
    if action.nargs is None:
        return index + 2 if index + 1 < len(words) else None
    if isinstance(action.nargs, int):
        end = index + 1 + action.nargs
        return end if end <= len(words) else None
    return None  # Variable-arity options require contextual parsing, not guesses.


def helper_action(argv):
    """Only the actual subcommand, after helper-wide options, selects an action."""
    helper = helper_argv(argv)
    if not helper:
        return "", []
    name, action = Path(helper[0]).name, helper[1:]
    parser = helper_parser(name if name in {"gh-plan.py", "gh-pr.py"} else "gh-plan.py")
    while action and action[0].startswith("-"):
        end = option_end(parser, action, 0)
        if end is None:
            break
        action = action[end:]
    return name, action


def ownership_command(argv):
    name, action = helper_action(argv)
    verb = "claim" if name == "gh-plan.py" else "create"
    owning = (name == "gh-plan.py" and action[:1] == ["claim"]
              or name == "gh-pr.py" and action[:1] == ["create"]
              or name == "gh" and action[:1] == ["pr"] and action[1:2] in (["create"], ["new"]))
    # Only the observed bare action help form is exempt. Elsewhere a flag-like
    # word may be an option value rather than a help request.
    help_only = action[-2:] in ([verb, "--help"], [verb, "-h"]) and action.count(verb) == 1
    if owning and name == "gh-plan.py":
        help_only = help_only or claim_read_only_help(argv)
    if owning and (name == "gh-pr.py" or name == "gh" and any(word in {"--help", "-h"} for word in action)):
        parsed = creation_arguments(argv)
        help_only = help_only or parsed is not None and getattr(parsed, "help_only", False)
    return owning and not help_only


def claim_read_only_help(argv):
    """Only the real parser can decide whether a help-shaped argument executes."""
    parser = helper_parser("gh-plan.py")
    claim_parser = next(item for item in parser._actions
                        if isinstance(item, argparse._SubParsersAction)).choices["claim"]
    words = helper_argv(argv)[1:]
    if not any(parsed and isinstance(parsed[0], argparse._HelpAction)
               for word in words for parsed in (parser_option(parser, word), parser_option(claim_parser, word))):
        return False
    with redirect_stdout(io.StringIO()):
        try:
            parser.parse_known_args(words)
        except SystemExit as error:
            return error.code == 0
        except Exception as error:
            parsing_error = getattr(sys.modules.get("github_api"), "ArgumentParsingError", ())
            return isinstance(error, parsing_error)
    return False


def target_repositories(records, harness, *, allowed_sessions=None):
    """Only actual claim destinations select mandatory ownership inventory.

    Foreign reads, PR creation and admission evidence do not create claims in
    their referenced repositories. PR URLs retain their separate provenance.
    """
    return {target["repository"] for target in claim_targets(records, harness, allowed_sessions=allowed_sessions)
            if target["repository"]}


def target_records(records, harness):
    return {(target["repository"], target["number"]) for target in claim_targets(records, harness)}


def claim_commands(name, value):
    return [argv for argv in shell_commands(name, value) if script(argv) == "gh-plan.py" and ownership_command(argv)]


def no_write_claims(records, harness):
    """A unique bound terminal refusal can prove that no ownership was written."""
    reports, single = {}, set()
    for record in records:
        for identifier, name, value in calls_and_results(record, harness)[0]:
            if len(claim_commands(name, value)) == 1 and isolated_command(name, value, "gh-plan.py"):
                single.add(identifier)
    for _, source, value in bound_outputs(records, harness, lambda n, v: bool(claim_commands(n, v)), isolated_polls=True):
        if value.get("operation") == CLAIM_OPERATION:
            reports.setdefault(source, []).append(value)
    return {source for source, values in reports.items() if source in single and len(values) == 1
            and values[0].get("ok") is False and values[0].get("write_outcome") == "not_started"
            and not values[0].get("completed_steps") and not values[0].get("claim_comment")}


def claim_operand(argv):
    """Read claim's positional using its maintained CLI option definitions."""
    name, action = helper_action(argv)
    if name != "gh-plan.py" or action[:1] != ["claim"]:
        return None
    parser = next(item for item in helper_parser(name)._actions
                  if isinstance(item, argparse._SubParsersAction)).choices["claim"]
    words, index = action[1:], 0
    while index < len(words):
        token = words[index]
        if token == "--":
            return words[index + 1] if index + 1 < len(words) else None
        if token.startswith("-"):
            end = option_end(parser, words, index)
            if end is None:
                return None
            index = end
        elif token in {">", ">>", "<", "<<", "2>", "2>>"}:
            return None
        else:
            return token
    return None


def claim_targets(records, harness, *, allowed_sessions=None):
    """Resolve explicit references; bind unscoped numbers to their own receipts."""
    if harness == "codex":
        records, _ = serial_ownership_records(records)
    def claiming(name, value):
        return bool(claim_commands(name, value))
    canonical = {}
    identities = ({record.get("payload", {}).get("id") for record in records if record.get("type") == "session_meta"}
                  if harness == "codex" else {record.get("sessionId") for record in records if record.get("sessionId")})
    identities = set(allowed_sessions) if allowed_sessions is not None else identities
    for _, source, value in bound_outputs(records, harness, claiming, isolated_polls=True):
        if (value.get("operation") != CLAIM_OPERATION
                or (value.get("claim") or {}).get("session") not in identities):
            continue
        issue = value.get("issue") or {}
        repo, number = issue.get("repo"), issue.get("number")
        url = ((value.get("claim_comment") or {}).get("comment") or {}).get("url") or (value.get("claim_comment") or {}).get("url")
        match = COMMENT.fullmatch(url or "")
        if match:
            repo, number = match[1], int(match[2])
        if isinstance(repo, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repo) and type(number) is int and number > 0:
            canonical.setdefault(source, set()).add((repo, number))
    targets = []
    no_writes = no_write_claims(records, harness)
    for record in records:
        calls, _ = calls_and_results(record, harness)
        for identifier, name, value in calls:
            if identifier in no_writes:
                continue
            commands = claim_commands(name, value)
            own = canonical.get(identifier, set())
            if len(commands) != 1 or len(own) != 1 or not isolated_command(name, value, "gh-plan.py"):
                own = set()
            for argv in commands:
                target = claim_operand(argv) or ""
                explicit = (re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/([1-9]\d*)", target)
                            or re.fullmatch(r"([\w.-]+/[\w.-]+)#([1-9]\d*)", target))
                if explicit:
                    resolved = {(explicit[1], int(explicit[2]))}
                elif re.fullmatch(r"#?[1-9]\d*", target):
                    number = int(target.removeprefix("#"))
                    normalized = helper_argv(argv)
                    repos = {normalized[i + 1] for i, token in enumerate(normalized[:-1])
                             if token == "-R" or repository_option(token) and "=" not in token}
                    repos.update(token.partition("=")[2] for token in normalized
                                 if repository_option(token) and "=" in token)
                    repos = {repo for repo in repos if re.fullmatch(r"[\w.-]+/[\w.-]+", repo)}
                    # Only isolated native output can establish an invocation's
                    # destination; another command may print a saved receipt.
                    resolved = {(repo, number) for repo in repos} or {
                        pair for pair in own if pair[1] == number} or {(None, number)}
                else:
                    resolved = own or {(None, None)}
                for repo, number in resolved:
                    targets.append({"invocation": identifier, "repository": repo, "number": number})
    return targets


def bound_outputs(records, harness, predicate, *, receipts=None, isolated_polls=False, opaque_calls=()):
    """Bind native outputs and process polls to an actual tool invocation."""
    calls, processes, background, polled, outstanding = {}, {}, set(), {}, {}
    opaque, required, deferred, visible = set(), {}, {}, {}
    serial_calls = {}
    parsed = {}
    for index, value in receipts or []:
        parsed.setdefault(index, []).append(value)
    for index, record in enumerate(records):
        invocations, results = calls_and_results(record, harness)
        for call_id, name, value in invocations:
            invoked = call_id in opaque_calls or predicate(name, value)
            value = arguments(value)
            polls = set()
            if name in {"write_stdin", "wait", "functions.wait"} and isinstance(value, dict):
                polls.update(str(value[k]) for k in ("session_id", "cell_id") if k in value)
            elif name in {"exec", "functions.exec"} and isinstance(value, str):
                if not isolated_polls or any(single_composed_call(value, method) for method in ("write_stdin", "wait")):
                    for method, args, _ in composed_calls(value):
                        if method in {"write_stdin", "wait"}:
                            polls.update(str(args[key]) for key in ("session_id", "cell_id") if key in args)
            if isolated_polls and len(polls) > 1:
                polls.clear()
            source = call_id if invoked else next((processes[p] for p in polls if p in processes), None)
            calls[call_id] = source
            polled[call_id] = polls if not invoked else set()
            if invoked and name == "Bash" and isinstance(value, dict) and value.get("run_in_background"):
                background.add(source)
            if invoked:
                if call_id in opaque_calls or opaque_ownership(name, value):
                    opaque.add(source)
                # A shell's result cannot attest completion of detached writers.
                for command in shell_texts(name, value):
                    try:
                        lexer = shell_lexer(shell_code(command), posix=False)
                        lexer.whitespace_split = True
                        if "&" in list(lexer):
                            opaque.add(source)
                    except ValueError:
                        opaque.add(source)
                if name in {"exec", "functions.exec"}:
                    serial = composed_calls(value)
                    if not serial:
                        opaque.add(source)  # Hidden/computed calls cannot borrow printed read results.
                    if len(serial) > 1 or any(stdout for _, _, stdout in serial):
                        serial_calls[source] = serial
                    required[source] = (sum(not stdout for _, _, stdout in serial)
                        if serial and not any(stdout and predicate(method, args) for method, args, stdout in serial)
                        else len(list(shell_texts(name, value))))
                yield index, source, {"_attempt": True}
        for result in results:
            source = calls.get(result)
            if source is None:
                continue
            values = [v for v in (parsed.get(index, []) if receipts is not None else
                                   (v for _, v in raw_outputs([record], harness, call_id=result)))
                      if v.get("_tool_call_id") == result]
            # Stock shell/orchestration tools may expose their handle as text.
            raw = json.dumps((record.get("payload") or {}).get("output")) if harness == "codex" else json.dumps([
                b.get("content") for b in (record.get("message") or {}).get("content", [])
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == result])
            if source in serial_calls and result == source:
                blocks, _ = serial_native_chunk(record)
                wrappers = serial_native_results(blocks, serial_calls[source])
                values = [v for wrapper in wrappers or [] if wrapper is not None for v in envelopes(wrapper)]
                # Read stdout cannot supply handles or terminal execution proof.
                # A partial/deferred serial cell cannot prove the missing writer.
                raw = ""
                if wrappers is None:
                    opaque.add(source)
            handles = {str(v["session_id"]) for v in values if isinstance(v.get("session_id"), int)}
            outer = set(re.findall(r"Script running with cell ID ([\w-]+)", raw))
            handles.update(re.findall(r"(?:Process running with session ID|Script running with cell ID) ([\w-]+)", raw))
            if source in required and (result == source or polled.get(result, set()) & deferred.get(source, set())):
                visible[source] = visible.get(source, 0) + sum(
                    not v.get("operation") and (isinstance(v.get("session_id"), int) or isinstance(v.get("exit_code"), int))
                    for v in values)
                if outer:
                    deferred.setdefault(source, set()).update(outer)
                elif visible[source] < required[source]:
                    # Dropped inner results leave writers with no recoverable handle.
                    opaque.add(source)
            for handle in handles:
                processes[handle] = source
            active = outstanding.setdefault(source, set())
            active.difference_update(polled.get(result, set()))
            active.update(handles)
            native_task = record.get("toolUseResult")
            task = (native_task.get("backgroundTaskId") or native_task.get("taskId")) if harness == "claude" and isinstance(native_task, dict) else None
            yield index, source, {"_result": True, "_pending": bool(active) or bool(task) or source in background or source in opaque,
                                  "_background_task": task, "_native_output": raw}
            for value in values:
                yield index, source, value


def check_outputs(records, harness, *, receipts=None):
    """A saved report or tool prompt cannot attest a fresh executing check."""
    predicate = lambda name, value: any(script(argv) == "check_claims.py" for argv in shell_commands(name, value))
    first, boundaries, candidates, totals = {}, {}, {}, {}
    for index, record in enumerate(records):
        # Claude splits one API message across native JSONL records. Calls in
        # that message share a launch boundary even if results are interleaved.
        message = (record.get("message") or {}).get("id") if harness == "claude" and record.get("type") == "assistant" else None
        group = ("message", message) if message else ("record", index)
        boundaries[index] = first.setdefault(group, index)
        calls, _ = calls_and_results(record, harness)
        for identifier, name, value in calls:
            totals[group] = totals.get(group, 0) + bool(predicate(name, value))
            if isolated_command(name, value, "check_claims.py"):
                candidates[identifier] = group
    isolated = {identifier for identifier, group in candidates.items() if totals[group] == 1}
    parsed = list(outputs(records, harness)) if receipts is None else receipts
    counts = {}
    for index, value in parsed:
        if value.get("operation") == CHECK_OPERATION:
            counts[index] = counts.get(index, 0) + 1
    starts = {}
    for index, source, value in bound_outputs(records, harness, predicate, receipts=parsed, isolated_polls=True):
        if value.get("_attempt"):
            starts[source] = boundaries[index]
            yield starts[source], {"operation": CHECK_OPERATION, "_attempt": True}
        elif (source in isolated and counts.get(index) == 1
              and value.get("operation") == CHECK_OPERATION and not value.get("_tool_error")):
            yield index, {**value, "_invoked_at": starts[source]}


def unscoped_claim_invocations(records, harness):
    """A different claim's receipt cannot fill a redirected invocation's gap."""
    return [target["invocation"] for target in claim_targets(records, harness) if target["repository"] is None]


def sources(records, harness, session, *, parsed=None):
    """Return every source claim actually reported by the maintained claim helper."""
    found, errors = {}, []
    for index, value in outputs(records, harness) if parsed is None else parsed:
        if value.get("operation") != CLAIM_OPERATION:
            continue
        record = value.get("claim") or {}
        if session is None and record.get("session"):
            errors.append("claim receipt lacks unambiguous native transcript identity")
            continue
        if record.get("session") != session:
            continue
        receipt = value.get("claim_comment") or {}
        url = (receipt.get("comment") or {}).get("url") or receipt.get("url")
        match = COMMENT.fullmatch(url or "")
        if match:
            key = (match[1], int(match[2]), int(match[3]))
            found[key] = {"repository": key[0], "record": key[1], "claim_comment": key[2], "index": index}
        elif value.get("ok") or value.get("write_outcome") in {"unknown", "confirmed"} or "post_claim" in value.get("completed_steps", []):
            issue = value.get("issue") or {}
            if (value.get("ok") and "post_claim" not in value.get("completed_steps", [])
                    and any(key[:2] == (issue.get("repo"), issue.get("number")) for key in found)):
                continue  # Confirmed renewal of an already recorded source.
            errors.append("claim write has no complete source receipt")
    return list(found.values()), errors


def creation_command(argv):
    return ownership_command(argv) and script(argv) in {"gh", "gh-pr.py"}


def created_pr_evidence(records, harness):
    """Bind create URLs and definitive no-write reports to isolated invocations."""
    if harness == "codex":
        records, _ = serial_ownership_records(records)
    def creating(name, value):
        return any(creation_command(argv) for argv in shell_commands(name, value))
    isolated = {identifier: helper for record in records
                for identifier, name, value in calls_and_results(record, harness)[0]
                for helper in ("gh", "gh-pr.py") if creating(name, value) and isolated_command(name, value, helper)}
    found, reports, terminal = {}, {}, set()
    for _, source, value in bound_outputs(records, harness, creating, isolated_polls=True):
        if source not in isolated or value.get("_attempt"):
            continue
        if value.get("_result") and not value.get("_pending"):
            terminal.add(source)
        operation = value.get("operation")
        if operation:
            if operation == "github.pr.create":
                reports.setdefault(source, []).append(value)
                if value.get("ok") is True and isinstance(value.get("url"), str):
                    if re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*", value["url"]):
                        found.setdefault(source, set()).add(value["url"])
            continue
        if isolated[source] != "gh":
            continue
        text = value.get("_native_output", "") if value.get("_result") else json.dumps(value)
        found.setdefault(source, set()).update(re.findall(r"https://github\.com/[^/\s\"\\]+/[^/\s\"\\]+/pull/[1-9]\d*", text))
    no_writes = {source for source, values in reports.items() if source in terminal and len(values) == 1
                 and values[0].get("ok") is False and values[0].get("write_outcome") == "not_started"
                 and not values[0].get("completed_steps") and not values[0].get("url")}
    return found, no_writes


def created_prs(records, harness):
    """Only isolated create commands and their own polls can supply PR URLs."""
    found, _ = created_pr_evidence(records, harness)
    return set().union(*found.values()) if found else set()


def creation_arguments(argv):
    """Read helper options with its parser, adapting the native gh aliases."""
    helper = helper_argv(argv)
    name, action = helper_action(argv)
    words = helper[1:]
    if name == "gh":
        # gh-pr mirrors gh create's long options; these are gh's short forms.
        aliases = {"-H": "--head", "-R": "--repo", "-t": "--title", "-b": "--body-file",
                   "--body": "--body-file", "-B": "--base", "-d": "--draft", "-f": "--fill",
                   "-l": "--label", "-r": "--reviewer", "-a": "--assignee", "-p": "--project",
                   "-m": "--milestone", "-T": "--template", "-F": "--body-file"}
        words, repository, cursor = [], [], 0
        create_parser = next(item for item in helper_parser("gh-pr.py")._actions
                             if isinstance(item, argparse._SubParsersAction)).choices["create"]
        tail = action[2:]
        while cursor < len(tail):
            token = tail[cursor]
            option, equal, inline = token.partition("=")
            option = aliases.get(option, option)
            if token[:2] in {"-H", "-R"} and len(token) > 2 and not equal:
                option, equal, inline = aliases[token[:2]], "=", token[2:]
            normalized = option + ("=" + inline if equal else "")
            if option == "--repo":
                if equal:
                    repository = ["--repo", inline]
                elif cursor + 1 < len(tail):
                    repository = ["--repo", tail[cursor + 1]]
                    cursor += 1
                cursor += 1
                continue
            end = option_end(create_parser, [normalized, *tail[cursor + 1:]], 0)
            if end is None:
                break  # Preserve known scope before an unfamiliar gh-only option.
            words.extend([normalized, *tail[cursor + 1:cursor + end]])
            cursor += end
        words = [*repository, "create", *words]
    try:
        with redirect_stdout(io.StringIO()):
            args, _ = helper_parser("gh-pr.py").parse_known_args(words)
    except SystemExit as error:
        return argparse.Namespace(help_only=True) if error.code == 0 else None
    except Exception as error:
        parsing_error = getattr(sys.modules.get("github_api"), "ArgumentParsingError", ())
        if name == "gh-pr.py" and isinstance(error, parsing_error):
            return argparse.Namespace(help_only=True)
        return None
    return args


def created_pr_targets(records, harness):
    """Command-scoped heads survive compound output without borrowing PR URLs."""
    if harness == "codex":
        records, _ = serial_ownership_records(records)
    urls, no_writes = created_pr_evidence(records, harness)
    targets = []
    for index, record in enumerate(records):
        for identifier, name, value in calls_and_results(record, harness)[0]:
            if identifier in no_writes:
                continue
            commands = [argv for argv in shell_commands(name, value) if creation_command(argv)]
            own = urls.get(identifier, set())
            url = next(iter(own)) if len(commands) == 1 and len(own) == 1 else None
            for argv in commands:
                args = creation_arguments(argv)
                if args is not None and (getattr(args, "dry_run", False) or getattr(args, "help_only", False)):
                    continue
                repository, head = (getattr(args, "repo", None), getattr(args, "head", None)) if args is not None else (None, None)
                if not isinstance(repository, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
                    repository = url.split("/")[3] + "/" + url.split("/")[4] if url else None
                head_owner = repository.split("/")[0] if repository else None
                if not isinstance(head, str) or any(symbol in head for symbol in "$`\n"):
                    head = None
                elif ":" in head:
                    head_owner, head = head.split(":", 1)
                targets.append({"invocation": identifier, "index": index, "repository": repository,
                                "head": head, "head_owner": head_owner, "url": url})
    return targets


def acceptance(records, harness, session, *, histories=None, history_error=None):
    try:
        if history_error:
            return {"clear": False, "reason": "child_history_unavailable", "detail": history_error}
        import claim_history
        if histories is None and claim_history.delegated(records, harness):
            return {"clear": False, "reason": "child_history_unavailable"}
        return _acceptance(records, harness, session, histories)
    except (ReceiptCoverageError, RecursionError):
        return {"clear": False, "reason": "receipt_coverage_exceeded"}


def _acceptance(records, harness, session, histories):
    """A claimed session needs fresh passing evidence for all its source claims.

    Plain safe-exit prose stays recognizable; it is insufficient for shutdown
    acceptance when this native history contains claim ownership.
    """
    parsed = list(outputs(records, harness))
    histories = histories or [{"session_id": session, "harness": harness, "records": records}]
    sessions = {h["session_id"] for h in histories}
    expected, errors = [], []
    root_expected = []
    child_activity, pending, root_state = False, False, None
    file_candidates = ownership_file_candidates(histories)
    for node in histories:
        node_parsed = parsed if node["records"] is records and node["harness"] == harness else list(outputs(node["records"], node["harness"]))
        for native in sessions:
            found, missing = sources(node["records"], node["harness"], native, parsed=node_parsed)
            expected.extend(found)
            errors.extend(missing)
            if node is histories[0]:
                root_expected.extend(found)
        state = ownership_state(node["records"], node["harness"], file_candidates=file_candidates)
        if node["records"] is records and node["harness"] == harness:
            root_state = state
        pending = pending or state["pending"]
        if node is not histories[0] and state["indices"]:
            child_activity = True
    if pending:
        return {"clear": False, "reason": "ownership_activity_incomplete"}
    activity = (root_state if root_state is not None else ownership_state(records, harness))["indices"]
    if not expected and not errors and not activity and not child_activity:
        return {"clear": True, "reason": "no_claim_receipts"}
    keys = {(r["repository"], r["record"], r["claim_comment"]) for r in expected}
    last_claim = max([r["index"] for r in root_expected] + activity + [-1])
    no_writes = no_write_claims(records, harness)
    no_write_indices = {i for i, source, _ in bound_outputs(records, harness, lambda n, v: bool(claim_commands(n, v)), receipts=parsed)
                        if source in no_writes}
    last_claim = max([last_claim, *(i for i, v in parsed if v.get("operation") == CLAIM_OPERATION and i not in no_write_indices
                                  or v.get("operation") == "github.pr.create" and v.get("ok"))])
    checks = list(check_outputs(records, harness, receipts=parsed))
    last_attempt = max((i for i, v in checks if v.get("_attempt")), default=-1)
    for index, value in reversed(checks):
        started = value.get("_invoked_at", -1)
        if (value.get("operation") != CHECK_OPERATION or value.get("session_id") != session
                or value.get("harness") != harness or started <= last_claim
                or started < last_attempt or index <= started):
            continue
        coverage = value.get("coverage") or {}
        observed = {(r.get("repository"), r.get("record"), r.get("claim_comment"))
                    for r in value.get("released_claims", []) if isinstance(r, dict)}
        required = {(h["session_id"], h["transcript"], h["digest"]) for h in histories[1:]}
        covered = {(h.get("session_id"), h.get("transcript"), h.get("digest")) for h in coverage.get("histories", [])}
        clear = (value.get("ok") is True and value.get("claims_clear") is True
                 and coverage.get("complete") is True and keys <= observed
                 and required <= covered and (not required or sessions <= set(coverage.get("session_ids", [])))
                 and (not errors or value.get("receipt_reconciliation") is True))
        return {"clear": clear, "reason": "verified_claim_disposition" if clear else "claim_disposition_not_clear"}
    return {"clear": False, "reason": "claim_disposition_not_checked"}
