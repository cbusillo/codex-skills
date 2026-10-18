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
from pathlib import Path

CHECK_OPERATION = "work-closeout.claims"
CLAIM_OPERATION = "github.plan.claim"
COMMENT = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/([1-9]\d*)#issuecomment-([1-9]\d*)")
SHELL_FIELD = re.compile(r'''\btools\.exec_command\s*\(\s*\{(?:(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)|[^{}"'`])*?\b(?:cmd|command)\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')(?=\s*[,}])''')
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


def outputs(records, harness):
    for index, record in enumerate(records):
        if harness == "codex":
            payload = record.get("payload") or {}
            if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
                for value in envelopes(payload.get("output")):
                    yield index, {**value, "_tool_call_id": payload.get("call_id")}
        else:
            message = record.get("message")
            for block in (message.get("content") or []) if isinstance(message, dict) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    for value in envelopes(block.get("content")):
                        yield index, {**value, "_tool_call_id": block.get("tool_use_id"), "_tool_error": block.get("is_error") is True}


def calls_and_results(record, harness):
    payload = record.get("payload") or {}
    if harness == "codex":
        calls = [(payload.get("call_id"), payload.get("name"), payload.get("arguments") or payload.get("input"))] if payload.get("type") in {"function_call", "custom_tool_call"} else []
        results = [payload.get("call_id")] if payload.get("type") in {"function_call_output", "custom_tool_call_output"} else []
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
    if name in {"exec_command", "Bash"} and isinstance(value, dict):
        texts = [value.get("cmd") or value.get("command") or ""]
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


def shell_commands(name, value):
    """Only shell tools execute commands; tool prompts and echo operands do not."""
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
                        yield command[1:] if command[0] == "nohup" else command
                    command = []
                else:
                    command.append(token)
            if command:
                yield command[1:] if command[0] == "nohup" else command
        except ValueError:
            continue


def shell_code(text, *, bodies=None):
    """Exclude heredoc bodies, retaining commands on both sides of them."""
    code, pending, header = [], [], ""
    for line in text.splitlines(keepends=True):
        if pending:
            delimiter, tabs, expands, executes, body = pending[0]
            if (line.lstrip("\t") if tabs else line).rstrip("\r\n") == delimiter:
                if executes and OWNERSHIP_TEXT.search("".join(body)):
                    raise ValueError("unparsed executable heredoc")
                if bodies is not None:
                    bodies.append("".join(body))
                pending.pop(0)
            elif expands and ("$(" in line or "`" in line):
                raise ValueError("unparsed expanding heredoc")
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
            consumers = shell_lexer(header)
            consumers.whitespace_split = True
            executes = any(executable_wrapper([word]) for word in consumers)
            pending.append((delimiter[0], tabs, not any(c in raw for c in "'\"\\"), executes, []))
        code.append(header)
        header = ""
    if pending or header:
        raise ValueError("unparsed heredoc body")
    return "".join(code)


def opaque_ownership(name, value):
    """Unparsed potentially writing code is uncertainty, never proof of no write."""
    value = arguments(value)
    for text in shell_texts(name, value):
        bodies = []
        try:
            shell_code(text, bodies=bodies)
        except ValueError:
            if OWNERSHIP_TEXT.search(text):
                return True
        for argv in shell_commands("exec_command", {"cmd": text}):
            if executable_wrapper(argv) and (OWNERSHIP_TEXT.search(" ".join(argv))
                                            or any(OWNERSHIP_TEXT.search(body) for body in bodies)):
                return True
    if name in {"exec", "functions.exec"} and isinstance(value, str) and "tools.exec_command" in value:
        unparsed = SHELL_FIELD.sub("", value)
        if re.search(r"\btools\.exec_command\s*\(", unparsed):
            return bool(re.search(r"gh-plan\.py", unparsed) and re.search(r"\bclaim\b", unparsed)
                        or re.search(r"gh-pr\.py|\bgh\s+pr\b", unparsed) and re.search(r"\bcreate\b", unparsed))
    return False


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
            yield match[1], match[2]
    for text in shell_texts(name, value):
        if not OWNERSHIP_TEXT.search(text):
            continue
        try:
            lexer = shell_lexer(shell_code(text))
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            continue  # Existing opaque ownership handles unparsed shell text.
        for index, token in enumerate(tokens[:-1]):
            if token in {">", ">>"}:
                yield tokens[index + 1], text


def ownership_file_candidates(histories):
    return {Path(path).name for node in histories for record in node["records"]
            for _, name, value in calls_and_results(record, node["harness"])[0]
            for path, content in file_writes(name, value) if path and OWNERSHIP_TEXT.search(content)}


def file_ownership_executions(records, harness, *, file_candidates=()):
    """Link script candidates to later executions across native tool calls."""
    candidates, invocations = set(file_candidates), set()
    for record in records:
        for identifier, name, value in calls_and_results(record, harness)[0]:
            for path, content in file_writes(name, value):
                if path and OWNERSHIP_TEXT.search(content):
                    candidates.add(Path(path).name)
            if not candidates:
                continue
            commands = list(shell_commands(name, value))
            operands = []
            for argv in commands:
                redirected = False
                for word in argv:
                    if redirected:
                        redirected = False
                    elif word in {">", ">>", "<<", "<<<"}:
                        redirected = True
                    else:
                        operands.append(word)
            referenced = {Path(word).name for word in operands} & candidates
            if referenced and (any(executable_wrapper(argv) or argv[0] in {"source", "."} for argv in commands)
                               or any(Path(argv[0]).name in candidates for argv in commands)):
                invocations.add(identifier)
    return invocations


def shell_lexer(text, *, posix=True, punctuation_chars=";&|<>\n"):
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
    if not argv:
        return ""
    name = Path(argv[0]).name
    if name == "uv" and len(argv) > 2 and argv[1] == "run":
        return Path(argv[2]).name
    if re.fullmatch(r"python[0-9.]*", name) and len(argv) > 1:
        return Path(argv[1]).name
    return ""


def composed_calls(value):
    """Read only a complete sequence of visible, literal, serial native calls."""
    native = arguments(value)
    if not isinstance(native, str):
        return []
    native = re.sub(r"\A// @exec:[^\n]*\n", "", native)
    literal = r'''(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|-?\d+|true|false|null)'''
    field = rf"\w+\s*:\s*{literal}"
    obj = rf"\{{\s*(?:{field}(?:\s*,\s*{field})*\s*,?)?\s*\}}"
    call = rf"text\(\s*(?P<paren>\()?\s*await\s+tools\.(?P<method>exec_command|write_stdin|wait)\(\s*(?P<args>{obj})\s*\)\s*(?(paren)\)\s*\.output)\s*\)\s*;?\s*"
    found, cursor = [], 0
    while cursor < len(native.rstrip()):
        match = re.compile(r"\s*" + call).match(native, cursor)
        if match is None:
            return []
        fields = {}
        for item in re.finditer(rf"(\w+)\s*:\s*({literal})", match["args"]):
            raw = item[2]
            try:
                fields[item[1]] = {"true": True, "false": False, "null": None}.get(raw) if raw in {"true", "false", "null"} else ast.literal_eval(raw)
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


def ownership_command(argv):
    verb = "claim" if script(argv) == "gh-plan.py" else "create"
    owning = (script(argv) == "gh-plan.py" and "claim" in argv
              or script(argv) == "gh-pr.py" and "create" in argv
              or argv[:3] == ["gh", "pr", "create"])
    # Only the observed bare action help form is exempt. Elsewhere a flag-like
    # word may be an option value rather than a help request.
    help_only = argv[-2:] in ([verb, "--help"], [verb, "-h"]) and argv.count(verb) == 1
    return owning and not help_only


def target_repositories(records, harness):
    repos = {target["repository"] for target in claim_targets(records, harness) if target["repository"]}
    for record in records:
        calls, _ = calls_and_results(record, harness)
        for _, name, value in calls:
            for argv in shell_commands(name, value):
                if script(argv) not in {"gh-plan.py", "gh-pr.py"} and argv[:2] not in (["gh", "pr"], ["gh", "issue"]):
                    continue
                for i, token in enumerate(argv):
                    repo = argv[i + 1] if token in {"--repo", "-R"} and i + 1 < len(argv) else token.removeprefix("--repo=") if token.startswith("--repo=") else ""
                    if re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                        repos.add(repo)
                    match = re.match(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/", token)
                    if match:
                        repos.add(match[1])
    return repos


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


def claim_targets(records, harness):
    """Resolve explicit references; bind unscoped numbers to their own receipts."""
    def claiming(name, value):
        return bool(claim_commands(name, value))
    canonical = {}
    for _, source, value in bound_outputs(records, harness, claiming):
        if value.get("operation") != CLAIM_OPERATION:
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
            for argv in commands:
                target = argv[argv.index("claim") + 1:]
                if not target:
                    continue
                explicit = (re.fullmatch(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/([1-9]\d*)", target[0])
                            or re.fullmatch(r"([^/\s]+/[^#\s]+)#([1-9]\d*)", target[0]))
                if explicit:
                    resolved = {(explicit[1], int(explicit[2]))}
                elif re.fullmatch(r"#?[1-9]\d*", target[0]):
                    number = int(target[0].removeprefix("#"))
                    repos = {argv[i + 1] for i, token in enumerate(argv[:-1]) if token in {"--repo", "-R"}}
                    repos.update(token.removeprefix("--repo=") for token in argv if token.startswith("--repo="))
                    # A native invocation can contain several shell commands.
                    # Its receipts do not identify which command produced them.
                    own = canonical.get(identifier, set()) if len(commands) == 1 else set()
                    resolved = {(repo, number) for repo in repos} or {
                        pair for pair in own if pair[1] == number} or {(None, number)}
                else:
                    continue
                for repo, number in resolved:
                    targets.append({"invocation": identifier, "repository": repo, "number": number})
    return targets


def bound_outputs(records, harness, predicate, *, receipts=None, isolated_polls=False, opaque_calls=()):
    """Bind native outputs and process polls to an actual tool invocation."""
    calls, processes, background, polled, outstanding = {}, {}, set(), {}, {}
    opaque, required, deferred, visible = set(), {}, {}, {}
    serial_calls = {}
    parsed = {}
    for index, value in outputs(records, harness) if receipts is None else receipts:
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
                    polls.update(re.findall(r'''\b(?:session_id|cell_id)\s*:\s*["']?([\w-]+)''', value))
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
            values = [v for v in parsed.get(index, []) if v.get("_tool_call_id") == result]
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
                                  "_background_task": task}
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


def created_prs(records, harness):
    found = {value["url"] for _, value in outputs(records, harness)
            if value.get("operation") == "github.pr.create" and value.get("ok") is True
            and isinstance(value.get("url"), str)
            and re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*", value["url"])}
    def creating(name, value):
        return any(argv[:3] == ["gh", "pr", "create"] for argv in shell_commands(name, value))
    for index, _, value in bound_outputs(records, harness, creating):
        if value.get("_result"):
            found.update(re.findall(r"https://github\.com/[^/\s\"\\]+/[^/\s\"\\]+/pull/[1-9]\d*", json.dumps(records[index])))
    return found


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
