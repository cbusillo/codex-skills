# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read literal native claim/check receipts; current evidence owns uncertainty.

Generated-source interpretation and covered-write retirement are intentionally
absent. Canonical GitHub discussion owns claims, and native liveness is reread
by callers rather than inferred from an immutable execution history.
"""
from __future__ import annotations

import copy
import json
import re
import shlex
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

CHECK_OPERATION = "work-closeout.claims"
CLAIM_OPERATION = "github.plan.claim"
_SERIAL_NATIVE_RESULT = object()
HIDDEN_UV_RUN_OPTIONS = {"--python-preference": True, "--native-tls": False}
COMMENT = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/([1-9]\d*)#issuecomment-([1-9]\d*)")
JS_STRING = r'''(?:"(?:[^"\\\r\n]|\\(?:\r\n|[\s\S]))*"|'(?:[^'\\\r\n]|\\(?:\r\n|[\s\S]))*'|(?:String\.raw\s*)?`(?:[^`\\]|\\[\s\S])*`)'''

class ReceiptCoverageError(ValueError):
    pass


def unwrapped_command(argv, *, stop_at=()):
    """Recognize literal launchers, without executing or interpreting programs."""
    argv = list(argv)
    for _ in range(16):
        if not argv or Path(argv[0]).name in stop_at:
            return argv
        name = Path(argv[0]).name
        if name == "uv":
            return uv_run_argv(argv)
        if name in {"command", "exec", "nohup"}:
            argv = argv[1:]
        elif name in {"python", "python3"} or re.fullmatch(r"python3\.\d+", name):
            return argv[1:] if len(argv) > 1 and not argv[1].startswith("-") else argv
        elif name == "env":
            argv = argv[1:]
            while argv and re.fullmatch(r"[A-Za-z_]\w*=.*", argv[0]):
                argv = argv[1:]
        elif name == "timeout" and len(argv) > 2 and not argv[1].startswith("-"):
            argv = argv[2:]
        else:
            return argv
    return argv


def shell_texts(name, value):
    value = arguments(value)
    if name in {"exec", "functions.exec"}:
        for method, args, _ in composed_calls(value):
            if method == "exec_command":
                yield args.get("cmd") or args.get("command") or ""
    elif name in {"exec_command", "functions.exec_command", "Bash", "shell", "shell_command", "local_shell_call"} and isinstance(value, dict):
        text = value.get("cmd") or value.get("command") or ""
        if isinstance(text, list):
            text = shell_c_body(text) or shlex.join(text)
        if isinstance(text, str):
            yield text


def shell_commands(name, value, *, depth=0):
    """Literal command scope is a hint for inventory, never write coverage."""
    for text in shell_texts(name, value):
        lexer = shell_lexer(text)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        command = []
        try:
            for word in lexer:
                if word in {"<<", "<<-"}:
                    if command:
                        body = shell_c_body(unwrapped_command(command))
                        if body is not None and depth < 16:
                            yield from shell_commands("exec_command", {"cmd": body}, depth=depth + 1)
                        else:
                            yield command
                    break  # Program input is not a second shell command.
                if word and all(c in ";&|\n" for c in word):
                    if command:
                        yield command
                    command = []
                elif word and all(c in "<>" for c in word):
                    next(lexer, None)  # Redirection destination, not argv.
                else:
                    command.append(word)
            else:
                if command:
                    body = shell_c_body(unwrapped_command(command))
                    if body is not None and depth < 16:
                        yield from shell_commands("exec_command", {"cmd": body}, depth=depth + 1)
                    else:
                        yield command
        except ValueError:
            continue  # Unparsed execution remains native uncertainty.


def execution_call(name):
    return name in {"exec", "functions.exec", "exec_command", "functions.exec_command",
                    "Bash", "shell", "shell_command", "local_shell_call"}


def opaque_ownership(name, value):
    # The input language is deliberately bounded. No source program is run or
    # modeled to prove that a computed cell cannot write. Live evidence can
    # resolve this uncertainty, including after its native session exits.
    return name in {"exec", "functions.exec"} and not composed_calls(value)


def ownership_state(records, harness, **_unused):
    """Retain possible executions; a fresh caller resolves current liveness."""
    positions = list(range(len(records)))
    if harness == "codex":
        records, positions = serial_ownership_records(records)
    indices, pending = [], set()
    def possible(name, value):
        serial = composed_calls(value) if name in {"exec", "functions.exec"} else []
        return (execution_call(name) and (not serial or any(method == "exec_command" for method, _, _ in serial))
                and not isolated_command(name, value, "check_claims.py"))
    for index, source, value in bound_outputs(records, harness, possible, isolated_polls=True):
        if value.get("_attempt"):
            pending.add(source)
            indices.append(index)
        elif value.get("_result"):
            indices.append(index)
            if not value.get("_pending"):
                pending.discard(source)
    return {"indices": [positions[index] for index in indices], "pending": bool(pending),
            "pending_invocations": sorted(pending)}


def javascript_string(raw):
    """Decode one static JavaScript literal; interpolation is not literal data."""
    if re.fullmatch(JS_STRING, raw) is None:
        raise ValueError("not a static JavaScript string token")
    tagged = raw.startswith("String.raw")
    if tagged:
        raw = raw[len("String.raw"):].lstrip()
    template = raw.startswith("`")
    text = raw[1:-1].replace("\r\n", "\n").replace("\r", "\n")
    result, cursor = [], 0
    while cursor < len(text):
        char = text[cursor]
        cursor += 1
        if template and char == "$" and text[cursor:cursor + 1] == "{":
            raise ValueError("interpolated template is not static")
        if char != "\\":
            result.append(char)
            continue
        if cursor == len(text):
            raise ValueError("unterminated escape")
        escaped = text[cursor]
        cursor += 1
        if tagged:
            result.extend(("\\", escaped))
        elif escaped in {"\n", "\u2028", "\u2029"}:
            continue
        elif escaped in "0123456789":
            if escaped != "0" or re.match(r"[0-9]", text[cursor:cursor + 1]) is not None:
                raise ValueError("legacy numeric escape is not a module literal")
            result.append("\0")
        elif escaped == "x":
            digits = text[cursor:cursor + 2]
            if re.fullmatch(r"[0-9a-fA-F]{2}", digits) is None:
                raise ValueError("invalid hexadecimal escape")
            result.append(chr(int(digits, 16)))
            cursor += 2
        elif escaped == "u":
            if text[cursor:cursor + 1] == "{":
                end = text.find("}", cursor + 1)
                digits = text[cursor + 1:end] if end >= 0 else ""
                if not re.fullmatch(r"[0-9a-fA-F]+", digits) or int(digits, 16) > 0x10ffff:
                    raise ValueError("invalid Unicode code point escape")
                cursor = end + 1
            else:
                digits = text[cursor:cursor + 4]
                if re.fullmatch(r"[0-9a-fA-F]{4}", digits) is None:
                    raise ValueError("invalid Unicode escape")
                cursor += 4
            result.append(chr(int(digits, 16)))
        else:
            result.append({"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}.get(escaped, escaped))
    # JavaScript strings store UTF-16 code units. Combine escaped surrogate pairs
    # as JSON/Node do, retaining isolated surrogate units without rejecting them.
    return "".join(result).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


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
                or any(script(argv) in {"gh-plan.py", "gh-pr.py", "check_claims.py"}
                       for argv in shell_commands(name, value)))
    isolated = {identifier: helper for record in records
                for identifier, name, value in calls_and_results(record, harness)[0]
                for helper in ("gh-plan.py", "gh-pr.py") if isolated_command(name, value, helper)}
    for index, source, value in bound_outputs(records, harness, relevant, isolated_polls=True,
                                         opaque_calls=()):
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


def shell_lexer(text, *, posix=True, punctuation_chars=";&|<>()\n"):
    # In a shell, # begins a comment only at the start of an unquoted word.
    # shlex's default also cuts OWNER/REPO#N in half.
    chars, quote, escaped, boundary, comment = [], None, False, True, False
    for char in text:
        if comment:
            if char != "\n":
                continue
            comment = False
        if escaped:
            chars.append(char)
            escaped, boundary = False, False
            continue
        if char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and boundary:
            comment = True
            continue
        chars.append(char)
        boundary = quote is None and (char.isspace() or char in ";&|()")
    lexer = shlex.shlex("".join(chars), posix=posix, punctuation_chars=punctuation_chars)
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
    literal = rf'''(?:{JS_STRING}|-?\d+|true|false|null)'''
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
                fields[item[1].strip("\"'")] = {"true": True, "false": False, "null": None}.get(raw) if raw in {"true", "false", "null"} else int(raw) if re.fullmatch(r"-?\d+", raw) else javascript_string(raw)
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


def serial_native_bindings(records):
    """Bind fresh prints to their actual composed launch, including outer waits.

    A waited cell may itself be a process poll; its own stdout projection owns
    the result contract. Single-call launches retain their original receipt IDs.
    """
    launches, cells, polls, printed, bindings = {}, {}, {}, {}, {}
    for index, record in enumerate(records):
        calls, results = calls_and_results(record, "codex")
        for identifier, name, value in calls:
            serial = composed_calls(value) if name in {"exec", "functions.exec"} else []
            if serial:
                launches[identifier] = serial
            args = arguments(value)
            if name in {"wait", "functions.wait"} and isinstance(args, dict):
                source = cells.get(str(args.get("cell_id")))
                if source is not None:
                    polls[identifier] = source
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
                bindings[index, identifier] = source, launches[source], wrappers
    return bindings


def serial_ownership_records(records):
    """Split only verified multi-call completions, preserving receipt contracts."""
    expanded, positions, contexts, split_ids = [], [], {}, set()
    bindings = serial_native_bindings(records)
    for index, record in enumerate(records):
        calls, results = calls_and_results(record, "codex")
        for identifier, _, _ in calls:
            contexts[identifier] = {key: record[key] for key in ("timestamp", "cwd") if key in record}
        split = None
        for identifier in results:
            binding = bindings.get((index, identifier))
            if binding is not None and len(binding[1]) > 1:
                source, serial, wrappers = binding
                split_ids.add(source)
                split = []
                for part, ((method, args, _), wrapper) in enumerate(zip(serial, wrappers)):
                    part_id = f"{source}:serial:{part}"
                    split.append({**contexts[source], "payload": {"type": "function_call", "name": method, "call_id": part_id, "arguments": json.dumps(args)}})
                    # A stdout-only writer/poll has no native terminal wrapper.
                    # Retain its launch so it cannot become a false all-clear.
                    if wrapper is not None:
                        split.append({**({"timestamp": record["timestamp"]} if "timestamp" in record else {}),
                                      "_native_serial_result": _SERIAL_NATIVE_RESULT,
                                      "payload": {"type": "function_call_output", "call_id": part_id,
                                                  "output": [{"type": "input_text", "text": json.dumps(wrapper)}]}})
        if split is not None:
            expanded.extend(split)
            positions.extend([index] * len(split))
        else:
            expanded.append(record)
            positions.append(index)
    # Remove only launches whose result was actually split. An incomplete cell
    # retains the original pending/opaque behavior.
    kept = [(r, p) for r, p in zip(expanded, positions)
            if not any(identifier in split_ids for identifier, _, _ in calls_and_results(r, "codex")[0])]
    return [r for r, _ in kept], [p for _, p in kept]


def current_check_process(initial, current, *, pending_waits=False):
    """Bind one yielded checker to native startup metadata, never its stdout."""
    if current[:len(initial)] != initial:
        return None
    calls = [(index, call) for index, record in enumerate(initial)
             for call in calls_and_results(record, "codex")[0]
             if call[1] in {"exec", "functions.exec"}
             and single_composed_call(call[2], "exec_command")
             and isolated_command(call[1], call[2], "check_claims.py")]
    if not calls:
        return None
    launch_index, (identifier, _name, _value) = calls[-1]
    startups = [wrappers[0] for source, serial, wrappers in serial_native_bindings(current).values()
                if source == identifier and len(serial) == 1 and not serial[0][2]]
    if (len(startups) != 1 or type(startups[0].get("session_id")) is not int
            or startups[0]["session_id"] <= 0 or startups[0].get("exit_code") is not None):
        return None
    handle = startups[0]["session_id"]
    allowed, cells, waits, returned, completed = {identifier}, set(), {}, {}, {}
    thread = turn = None
    for index, record in enumerate(current):
        calls, results = calls_and_results(record, "codex")
        payload = record.get("payload") or {}
        if record.get("type") == "session_meta":
            thread = payload.get("id")
        if payload.get("type") == "task_started":
            turn = payload.get("turn_id")
        # Startup may finish after the caller has already polled. Validate the
        # entire suffix from the launch, including polls in the initial prefix.
        if index > launch_index:
            if payload.get("type") in {"task_started", "user_message"}:
                return None
            for call_id, method, args in calls:
                args = arguments(args)
                if method == "sleep" and payload.get("namespace") == "clock":
                    if (not isinstance(args, dict) or set(args) != {"duration_ms"}
                            or type(args["duration_ms"]) is not int or args["duration_ms"] <= 0
                            or not isinstance(thread, str) or not thread or not isinstance(turn, str) or not turn
                            or not isinstance(call_id, str) or not call_id or call_id in waits):
                        return None
                    waits[call_id] = (args["duration_ms"], thread, turn)
                    allowed.add(call_id)
                    continue
                if payload.get("namespace") not in {None, "functions"}:
                    return None
                if method in {"exec", "functions.exec"}:
                    serial = composed_calls(args)
                    if len(serial) != 1 or serial[0][2]:
                        return None
                    method, args, _ = serial[0]
                if not isinstance(args, dict):
                    return None
                if method in {"write_stdin", "functions.write_stdin"}:
                    if args.get("session_id") != handle or args.get("chars", "") != "":
                        return None
                elif method in {"wait", "functions.wait"}:
                    if args.get("cell_id") not in cells or args.get("terminate") is True:
                        return None
                else:
                    return None
                allowed.add(call_id)
        if record.get("type") == "response_item" and payload.get("type") == "function_call_output" and payload.get("call_id") in waits:
            call_id = payload["call_id"]
            returned[call_id] = returned.get(call_id, 0) + 1
        item = payload.get("item")
        if record.get("type") == "event_msg" and payload.get("type") == "item_completed" and isinstance(item, dict) and item.get("id") in waits:
            duration, owner, source_turn = waits[item["id"]]
            if (item.get("type") != "Extension" or item.get("kind") != "clock.sleep"
                    or type(item.get("durationMs")) is not int or item["durationMs"] != duration or payload.get("thread_id") != owner
                    or payload.get("turn_id") != source_turn
                    or type(payload.get("started_at_ms")) is not int
                    or type(payload.get("completed_at_ms")) is not int
                    or payload["completed_at_ms"] < payload["started_at_ms"]):
                return None
            completed[item["id"]] = completed.get(item["id"], 0) + 1
        if any(result in allowed for result in results):
            _, cell = serial_native_chunk(record)
            if cell:
                cells.add(cell)
    if any(count > 1 for count in (*returned.values(), *completed.values())):
        return None
    pending = {call_id for call_id in waits if returned.get(call_id) != 1 or completed.get(call_id) != 1}
    if len(pending) > 1 or pending and not pending_waits:
        return None
    return str(handle)


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


def bound_outputs(records, harness, predicate, *, receipts=None, isolated_polls=False, opaque_calls=()):
    """Bind native outputs and process polls to an actual tool invocation."""
    calls, processes, background, polled, outstanding = {}, {}, set(), {}, {}
    opaque, required, deferred, visible = set(), {}, {}, {}
    serial_calls = {}
    tasks, running, scheduled, task_tools = {}, {}, {}, {}
    parsed = {}
    for index, value in receipts or []:
        parsed.setdefault(index, []).append(value)
    for index, record in enumerate(records):
        if harness == "claude":
            from session_record import track_claude_background_tasks
            previous = set(running)
            track_claude_background_tasks(record, running, scheduled, task_tools)
            for task in previous - running.keys():
                source = tasks.pop(task, None)
                if source is not None and source not in tasks.values():
                    background.discard(source)
                    yield index, source, {"_result": True, "_pending": bool(outstanding.get(source)) or source in opaque}
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
                        lexer = shell_lexer(command, posix=False)
                        lexer.whitespace_split = True
                        if ("&" in list(lexer) or any((unwrapped_command(argv, stop_at={"coproc"}) or [])[:1] == ["coproc"]
                                for argv in shell_commands("exec_command", {"cmd": command}))):
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
            if task and len(results) == 1:
                tasks[task] = source
            yield index, source, {"_result": True, "_tool_call_id": result, "_pending": bool(active) or bool(task) or source in background or source in opaque,
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
    for node in histories:
        node_parsed = parsed if node["records"] is records and node["harness"] == harness else list(outputs(node["records"], node["harness"]))
        for native in sessions:
            found, missing = sources(node["records"], node["harness"], native, parsed=node_parsed)
            expected.extend(found)
            errors.extend(missing)
            if node is histories[0]:
                root_expected.extend(found)
        state = ownership_state(node["records"], node["harness"])
        if node["records"] is records and node["harness"] == harness:
            root_state = state
        pending = pending or state["pending"]
        if node is not histories[0] and state["indices"]:
            child_activity = True
    activity = (root_state if root_state is not None else ownership_state(records, harness))["indices"]
    if not pending and not expected and not errors and not activity and not child_activity:
        return {"clear": True, "reason": "no_claim_receipts"}
    keys = {(r["repository"], r["record"], r["claim_comment"]) for r in expected}
    last_claim = max([r["index"] for r in root_expected] + activity + [-1])
    last_claim = max([last_claim, *(i for i, v in parsed if v.get("operation") == CLAIM_OPERATION
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
