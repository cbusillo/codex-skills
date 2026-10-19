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


def stdin_language(argv):
    """Identify interpreters that read code from stdin, rather than a script argv."""
    original = list(argv)
    argv = unwrapped_command(argv)
    if not argv:
        return "argv" if original and Path(original[0]).name == "uv" and "run" in original else None
    name, words = Path(argv[0]).name, argv[1:]
    if argv == ["-"] and original and Path(original[0]).name == "uv":
        return "python"
    if name == "uv":
        return "argv" if words[:1] == ["run"] or words and words[0].startswith("-") and "run" in words else None
    if name in {"sudo", "doas", "flock", "ssh"}:
        children = list(executor_commands(argv))
        languages = {stdin_language(shlex.split(command)) for command in children}
        languages.discard(None)
        if languages:
            return next(iter(languages)) if len(languages) == 1 else "argv"
        if name == "ssh" and not children and len(argv) > 1 and not any(word in {"-V", "-G", "-Q", "-N", "-O"} for word in words):
            return "shell"
        return None
    stdin_paths = {"/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
    if name.startswith("$"):
        return "shell"
    if name == "source" or argv[0] == ".":
        return "shell" if words[:1] and words[0] in stdin_paths else None
    shell = name in {"bash", "sh", "zsh", "dash", "ksh"}
    python = re.fullmatch(r"python[0-9.]*", name) is not None
    if not (shell or python or name == "node"):
        return "argv" if name == "xargs" else None
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
                return None
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
    if name in {"printf", "echo", "true", "false", ":", "gh", "gh-plan.py", "gh-pr.py"}:
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


def node_ownership(content):
    """Read literal child_process launches, excluding unrelated methods and data."""
    atoms = list(re.finditer(r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`''', content))
    code, literals = list(content), {}
    for atom in atoms:
        if not atom[0].startswith(("//", "/*")):
            literals[atom.start()] = atom
        code[atom.start():atom.end()] = " " * len(atom[0])
    code = "".join(code)
    declarations = {}
    for definition in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", code):
        start = definition.end() + len(re.match(r"\s*", content[definition.end():])[0])
        declarations.setdefault(definition[1], []).append((definition.end(), literals.get(start)))
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
        start = call.end() + len(re.match(r"\s*", content[call.end():])[0])
        literal = literals.get(start)
        if literal is None:
            identifier = re.match(r"[A-Za-z_$][\w$]*", content[start:])
            definitions = declarations.get(identifier[0], []) if identifier else []
            preceding = [value for position, value in definitions if position < call.start()]
            if preceding:
                literal = preceding[-1]
        if literal is None:
            if OWNERSHIP_TEXT.search(content):
                return True
            continue
        try:
            command = literal[0][1:-1] if literal[0].startswith("`") else ast.literal_eval(literal[0])
        except (ValueError, SyntaxError):
            if OWNERSHIP_TEXT.search(literal[0]):
                return True
            continue
        if kind in {"exec", "execSync"}:
            if executable_content(command) or opaque_ownership("exec_command", {"cmd": command}):
                return True
        else:
            # Node's execFile/spawn take the program and a literal argv array.
            tail = content[literal.end():]
            arguments = re.match(r"\s*,\s*(\[[\s\S]*?\])", tail)
            try:
                argv = ast.literal_eval(arguments[1]) if arguments else []
            except (ValueError, SyntaxError):
                continue
            if isinstance(argv, list) and all(isinstance(word, str) for word in argv) and ownership_command([command, *argv]):
                return True
    return False


def compound_stdin_language(tokens, source, at, spans, heredocs):
    """Read a group's actual commands, rather than treating its closing word as argv."""
    opens = {"(": ")", "{": "}", "while": "done", "until": "done", "for": "done",
             "select": "done", "if": "fi", "case": "esac"}
    stack, bounds = [], None
    command_start = True
    for index, token in enumerate(tokens):
        if token in opens and (token in {"(", "{"} or command_start):
            stack.append((index, opens[token]))
        elif stack and token == stack[-1][1]:
            begin, _ = stack.pop()
            if index == at or begin == at:
                bounds = (begin, index)
                break
        command_start = bool(token and all(c in ";&|\n" for c in token) or token in {"do", "then", "else"})
    if bounds is None:
        return None
    begin, end = bounds
    if begin == at and heredocs and end > min(heredocs):
        return None  # The redirection belongs to an inner command, not the group.
    if begin + 1 >= end or spans[begin + 1][0] is None or spans[end][0] is None:
        return None
    inner = source[spans[begin + 1][0]:spans[end][0]]
    lexer = shell_lexer(inner, posix=False)
    lexer.whitespace_split = True
    commands = []
    heredoc_languages(list(lexer), lexer.instream.getvalue(), command_kinds=commands)
    kinds = {kind for kind, _ in commands if kind}
    words = [unwrapped_command(argv) for _, argv in commands]
    # A read/eval loop executes the stream line by line, even though eval
    # itself does not read stdin. Ordinary data commands in groups stay data.
    if any(argv[:1] == ["read"] for argv in words) and any(
            argv[:1] == ["eval"] or shell_c_body(argv) and "$" in shell_c_body(argv) for argv in words):
        kinds.add("shell")
    if tokens[begin] == "case":
        for argv in words:
            if ")" in argv:
                kind = stdin_language(argv[argv.index(")") + 1:])
                if kind:
                    kinds.add(kind)
    return next(iter(kinds)) if len(kinds) == 1 else "argv" if kinds else None


def heredoc_languages(tokens, source, *, file_commands=None, command_kinds=None):
    """Bind each heredoc to its owning command and the pipeline's stdin readers.

    Redirections belong to one simple command. Later sequential commands cannot
    consume their bodies, and only the last stdin redirection supplies that
    command. A pipeline can pass the stream on to an interpreter unless that
    interpreter replaces its stdin with another redirection.
    """
    # posix=False retains the source spelling of words. Adjacency distinguishes
    # an fd prefix (2>log) from an ordinary numeric argument (2 >log).
    spans, cursor = [], 0
    for token in tokens:
        start = source.find(token, cursor)
        spans.append((start, start + len(token)) if start >= 0 else (None, None))
        if start >= 0:
            cursor = start + len(token)
    segments, current = [], []
    for index, token in enumerate(tokens):
        if token and all(c in ";&|\n" for c in token):
            segments.append((current, token))
            current = []
        else:
            current.append((index, token))
    segments.append((current, ""))
    commands = []
    for segment, separator in segments:
        argv, heredocs, cursor, last_word = [], [], 0, None
        descriptors = {0: "pipe-in", 1: "pipe-out", 2: None}
        while cursor < len(segment):
            index, token = segment[cursor]
            if token in {"<", ">", "<<", ">>", "<<<", "<>", "<&", ">&", ">|", "&>", "&>>"}:
                fd = 0 if token.startswith("<") else 1
                if (cursor and last_word == segment[cursor - 1][0] and segment[cursor - 1][1].isdigit()
                        and spans[segment[cursor - 1][0]][1] == spans[index][0]):
                    fd = int(segment[cursor - 1][1])
                    argv.pop()
                operand = segment[cursor + 1][1] if cursor + 1 < len(segment) else ""
                if token == "<<":
                    heredocs.append(index)
                    descriptors[fd] = index
                elif token in {"<&", ">&"} and operand.isdigit():
                    descriptors[fd] = descriptors.get(int(operand))
                elif token in {">", ">>", ">|", "&>", "&>>"}:
                    target = shlex.split(operand, comments=False)
                    descriptors[fd] = ("file", target[0]) if len(target) == 1 else None
                    if token.startswith("&>"):
                        descriptors[2] = descriptors[fd]
                else:
                    descriptors[fd] = None
                    if token.startswith("&>"):
                        descriptors[2] = None
                cursor += 2
                continue
            argv.extend(shlex.split(token, comments=False))
            last_word = index
            cursor += 1
        if file_commands is not None and isinstance(descriptors[1], tuple):
            file_commands.append({"target": descriptors[1][1], "input": descriptors[0],
                                  "heredocs": heredocs, "argv": argv,
                                  "position": len(commands),
                                  "source": " ".join(token for _, token in segment)})
        language = stdin_language(argv)
        if segment and (tokens[segment[0][0]] in {"(", "{", "}", ")", "done", "fi", "esac"}):
            language = compound_stdin_language(tokens, source, segment[0][0], spans, heredocs) or language
        if command_kinds is not None:
            command_kinds.append((language, argv))
        commands.append((language, heredocs, descriptors[0], separator,
                         descriptors[1] == "pipe-out" and forwards_stdin(argv)))
    if file_commands is not None:
        stream, sources = None, {}
        for position, (_, _, input_source, separator, piped_output) in enumerate(commands):
            source = stream if input_source == "pipe-in" else input_source
            sources[position] = source if isinstance(source, int) else None
            stream = source if piped_output and separator in {"|", "|&"} else None
        for command in file_commands:
            command["body"] = sources[command["position"]]
    languages = {}
    for position, (language, heredocs, input_source, separator, piped_output) in enumerate(commands):
        for index in heredocs:
            consumer = language if index == input_source else None
            downstream, pipe = position + 1, separator
            while consumer is None and index == input_source and piped_output and pipe in {"|", "|&"} and downstream < len(commands):
                next_language, _, next_input, pipe, piped_output = commands[downstream]
                if next_input != "pipe-in":
                    break
                consumer = next_language
                downstream += 1
            languages[index] = consumer
    return languages


def literal_producer_output(argv, fallback):
    """Resolve plain literal producers without executing a shell or format code."""
    argv = unwrapped_command(argv)
    if not argv:
        return fallback
    if Path(argv[0]).name == "echo" and (len(argv) == 1 or not argv[1].startswith("-")):
        return " ".join(argv[1:]) + "\n"
    if Path(argv[0]).name != "printf" or len(argv) < 2:
        return fallback
    format = argv[1]
    conversions = list(re.finditer(r"%%|%s", format))
    if "%" in re.sub(r"%%|%s", "", format) or re.search(r"\\[^\\abfnrtv]", format):
        return fallback
    escapes = dict(zip("\\abfnrtv", "\\\a\b\f\n\r\t\v"))
    format = re.sub(r"\\([\\abfnrtv])", lambda match: escapes[match[1]], format)
    count = sum(match[0] == "%s" for match in conversions)
    operands = argv[2:]
    if not count:
        return format % ()
    # Shell printf reuses its format and supplies empty missing string operands.
    groups = [operands[index:index + count] for index in range(0, len(operands), count)] or [[]]
    return "".join(format % tuple(group + [""] * (count - len(group))) for group in groups)


def shell_code(text, *, writes=None):
    """Exclude heredoc bodies, retaining commands on both sides of them."""
    # Quoted cat substitutions are a common CLI body argument. Their literal
    # contents must never participate in either shell lexing or '&' detection.
    substitution = re.compile(r"\$\(cat[ \t]+<<(-?)(['\"])([\w-]+)\2[ \t]*\n([\s\S]*?)\n[\t]*\3\r?\n\s*\)")
    text = substitution.sub("inert-heredoc-body", text)
    code, pending, header = [], [], ""
    file_commands, file_bodies = [], {}
    for line in text.splitlines(keepends=True):
        if pending:
            delimiter, tabs, expands, language, body, body_key = pending[0]
            if (line.lstrip("\t") if tabs else line).rstrip("\r\n") == delimiter:
                content = "".join(body)
                if expands:
                    if any(executable_content(expansion) or opaque_ownership("exec_command", {"cmd": expansion})
                           for expansion in shell_expansions(content, heredoc=True)):
                        raise ValueError("unparsed expanding heredoc")
                    # Backslashes protect $, `, \\ and newline during the outer
                    # shell's expansion, then disappear before the stdin reader.
                    content = re.sub(r"\\([\\$`\n])", lambda match: "" if match[1] == "\n" else match[1], content)
                if language == "shell":
                    owns = executable_content(content) or opaque_ownership("exec_command", {"cmd": content})
                elif language == "python":
                    owns = python_ownership(content)
                elif language == "node":
                    owns = node_ownership(content)
                else:
                    owns = language is not None and OWNERSHIP_TEXT.search(content)
                if owns:
                    raise ValueError("unparsed executable heredoc")
                file_bodies[body_key] = content
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
        header_files = [] if writes is not None else None
        languages = heredoc_languages(tokens, lexer.instream.getvalue(), file_commands=header_files) if "<<" in tokens or writes is not None else {}
        group = len(code)
        if header_files is not None:
            file_commands.extend((group, command) for command in header_files)
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
            pending.append((delimiter[0], tabs, not any(c in raw for c in "'\"\\"), languages.get(index), [], (group, index)))
        code.append(header)
        header = ""
    if pending or header:
        raise ValueError("unparsed heredoc body")
    if writes is not None:
        for group, command in file_commands:
            input_source = command["body"]
            if input_source is not None and forwards_stdin(command["argv"]):
                content = file_bodies[(group, input_source)]
            else:
                # Literal producers write their operands, rather than the
                # shell command that quoted those operands as data. A replaced
                # or unused stdin cannot lend a heredoc body to that output.
                content = literal_producer_output(command["argv"], command["source"])
            writes.append((command["target"], content))
    return "".join(code)


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
            if child and Path(child[0]).name in {"eval", "xargs", "node"} and OWNERSHIP_TEXT.search(" ".join(child[1:])):
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
        "ssh": {"-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w"},
    }.get(name)
    if value_options is None:
        return
    while cursor < len(argv):
        option, equal, _ = argv[cursor].partition("=")
        if name == "flock" and option in {"-c", "--command"}:
            if equal:
                yield argv[cursor].partition("=")[2]
            elif cursor + 1 < len(argv):
                yield argv[cursor + 1]
            return
        if option == "--":
            cursor += 1
            break
        if not option.startswith("-"):
            break
        cursor += 1
        takes_next = option in value_options
        if not option.startswith("--") and not takes_next:
            cluster = option[1:]
            for index, letter in enumerate(cluster):
                if "-" + letter in value_options:
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
        else:
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
        elif text[cursor:cursor + 2] == "$(" and quote != "'":
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


def unwrapped_command(argv):
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
            argv = child
        elif (((body := shell_c_body(argv)) is not None and (child := foreground_shell_argv(body)))
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
    """Python fixture strings are data; process-launch arguments are executable."""
    try:
        tree = ast.parse(textwrap.dedent(content))
    except SyntaxError:
        return False
    definitions = {node.targets[0].id: node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}
    def literal(node, seen=frozenset()):
        if isinstance(node, ast.Name) and node.id in definitions and node.id not in seen:
            return literal(definitions[node.id], seen | {node.id})
        if (isinstance(node, ast.Call) and ast.unparse(node.func) == "shlex.split"
                and len(node.args) == 1 and not node.keywords):
            text = literal(node.args[0], seen)
            if isinstance(text, str):
                try:
                    return shlex.split(text)
                except ValueError:
                    return None
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError):
            return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = ast.unparse(node.func)
        if function not in {"os.system", "os.popen", "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "exec", "eval"}:
            continue
        command = literal(node.args[0])
        if isinstance(command, (list, tuple)):
            if ownership_command(unwrapped_command(command)):
                return True
        elif isinstance(command, str) and executable_content(command):
            return True
    return False


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
    return python_fragment_ownership(content) if Path(path).suffix == ".py" else executable_content(content)


def executable_content(content):
    """Recognize executable ownership syntax, excluding quoted examples."""
    try:
        ast.parse(content)
    except SyntaxError:
        pass
    else:
        return python_ownership(content)
    return any(ownership_command(unwrapped_command(argv))
               for argv in shell_commands("exec_command", {"cmd": content}))


def executable_wrapper(argv):
    if not argv or script(argv) in {"gh-plan.py", "gh-pr.py"}:
        return False
    name = Path(argv[0]).name
    return (name in {"bash", "sh", "zsh", "dash", "ksh", "node", "uv", "env", "timeout", "eval", "xargs", "exec", "command", "nice", "time", "gh-plan.py", "gh-pr.py"}
            or re.fullmatch(r"python[0-9.]*", name) is not None
            or re.match(r"[A-Za-z_]\w*=", argv[0]) is not None)


def file_writes(name, value):
    """Read file provenance from native writes, never from current disk contents."""
    value = arguments(value)
    if name in {"Write", "Edit"} and isinstance(value, dict):
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
        for match in re.finditer(r"(?m)^\*\*\* (?:Add|Update) File: (.+)\n([\s\S]*?)(?=^\*\*\* |\Z)", patch):
            for hunk in re.split(r"(?m)^@@[^\n]*\n", match[2]):
                content = "\n".join(line[1:] for line in hunk.splitlines() if line.startswith(("+", " ")))
                if content:
                    yield match[1], content
    for text in shell_texts(name, value):
        if not OWNERSHIP_TEXT.search(text):
            continue
        writes = []
        try:
            shell_code(text, writes=writes)
        except ValueError:
            continue  # Existing opaque ownership handles unparsed shell text.
        yield from writes


def native_calls(records, harness):
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
                    yield identifier, method, args, args.get("workdir") or args.get("cwd") or cwd
                continue
            args = arguments(value)
            yield identifier, name, value, (args.get("workdir") or args.get("cwd") or args.get("working_directory") or cwd) if isinstance(args, dict) else cwd


def native_path(path, cwd):
    if posixpath.isabs(path):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(cwd, path)) if cwd else None


def ownership_file_candidates(histories):
    return {native_path(path, cwd) for node in histories
            for _, name, value, cwd in native_calls(node["records"], node["harness"])
            for path, content in file_writes(name, value)
            if path and native_path(path, cwd) and executable_file_content(path, content)}


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
    """Link script candidates to later executions across native tool calls."""
    candidates, invocations = set(file_candidates), set()
    for identifier, name, value, cwd in native_calls(records, harness):
        for path, content in file_writes(name, value):
            if path and native_path(path, cwd) and executable_file_content(path, content):
                candidates.add(native_path(path, cwd))
        if not candidates:
            continue
        commands = list(shell_commands(name, value))
        targets = [target for argv in commands for target in execution_targets(argv)]
        if (any(argv and Path(argv[0]).name in {"bash", "sh", "zsh"} for argv in commands)
                and any(Path(argv[0]).name == "cat" for argv in commands)):
            targets += [argv[i + 1] for argv in commands for i, word in enumerate(argv[:-1]) if word == "<"]
        if {native_path(word, cwd) for word in targets} & candidates:
            invocations.add(identifier)
    return invocations


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
    lexer = shlex.shlex("".join(cleaned), posix=posix, punctuation_chars=punctuation_chars)
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
    for index, record in enumerate(records):
        calls, results = calls_and_results(record, "codex")
        for identifier, name, value in calls:
            serial = composed_calls(value) if name in {"exec", "functions.exec"} else []
            if len(serial) > 1:
                launches[identifier] = serial
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
                    split.append({"payload": {"type": "function_call", "name": method, "call_id": part_id, "arguments": json.dumps(args)}})
                    # A stdout-only writer/poll has no native terminal wrapper.
                    # Retain its launch so it cannot become a false all-clear.
                    if wrapper is not None:
                        split.append({"payload": {"type": "function_call_output", "call_id": part_id, "output": json.dumps(wrapper)}})
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
    child_activity, pending = False, False
    file_candidates = ownership_file_candidates(histories)
    for node in histories:
        for native in sessions:
            found, missing = sources(node["records"], node["harness"], native)
            expected.extend(found)
            errors.extend(missing)
            if node is histories[0]:
                root_expected.extend(found)
        state = ownership_state(node["records"], node["harness"], file_candidates=file_candidates)
        pending = pending or state["pending"]
        if node is not histories[0] and state["indices"]:
            child_activity = True
    if pending:
        return {"clear": False, "reason": "ownership_activity_incomplete"}
    activity = ownership_state(records, harness)["indices"]
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
