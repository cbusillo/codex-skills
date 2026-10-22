# /// script
# requires-python = ">=3.12"
# dependencies = ["tree-sitter==0.26.0", "tree-sitter-bash==0.25.1"]
# ///
from __future__ import annotations

import ast
import json
import re
import shlex
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import claim_history
import github_plan_claim_log as claim_log
import tree_sitter_bash
from tree_sitter import Language, Node, Parser


def values(value: object) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from values(nested)
    elif isinstance(value, str):
        for line in value.splitlines():
            if not line.lstrip().startswith(("{", "[")):
                continue
            try:
                nested = json.loads(line)
            except ValueError:
                continue
            yield from values(nested)


def read_records(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                records.append(row)
    return records


def metadata(records: list[dict]) -> dict:
    return next(
        (row["payload"] for row in records if row.get("type") == "session_meta" and isinstance(row.get("payload"), dict)), {}
    )


def codex_children(records: list[dict]) -> set[str]:
    return {
        item["agent_thread_id"]
        for row in records
        if row.get("type") == "event_msg"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == "item_completed"
        and isinstance(item := row["payload"].get("item"), dict)
        and item.get("type") == "SubAgentActivity"
        and isinstance(item.get("agent_thread_id"), str)
    }


def indexed_children(databases: list, parent: str) -> dict[str, set[Path]]:
    children: dict[str, set[Path]] = {}
    try:
        for database in databases:
            for child, path in database.execute(
                "SELECT e.child_thread_id,t.rollout_path FROM thread_spawn_edges e "
                "LEFT JOIN threads t ON t.id=e.child_thread_id WHERE e.parent_thread_id=?",
                (parent,),
            ):
                children.setdefault(child, set())
                if path:
                    children[child].add(Path(path).expanduser().resolve())
    except sqlite3.Error as error:
        raise ValueError("native child index is unreadable; restore the claiming host's native index") from error
    return children


def indexed_paths(databases: list, session: str) -> set[Path]:
    try:
        return {
            Path(path).expanduser().resolve()
            for database in databases
            for (path,) in database.execute("SELECT rollout_path FROM threads WHERE id=?", (session,))
            if path
        }
    except sqlite3.Error as error:
        raise ValueError("native rollout index is unreadable; restore it or pass the child's native transcript") from error


def entry(session: str, path: Path, records: list[dict], identity: str) -> dict:
    return {"session_id": session, "transcript": str(path), "native_child": identity, "_transcript_records": records}


def codex_entries(root: dict, supplied: dict[str, set[Path]]) -> list[dict]:
    root_id, root_path = root["session_id"], Path(root["transcript"]).resolve()
    root_identity = metadata(read_records(root_path)).get("session_id", root_id)
    expanded = []
    pending = [(root_id, None, ())]
    visited = set()
    with claim_history.native_databases(root_path) as databases:
        while pending:
            native, parent, ancestors = pending.pop()
            if native in visited:
                continue
            if len(visited) >= 4096:
                raise ValueError("native session lineage exceeds its coverage bound")
            visited.add(native)
            paths = supplied.get(native) or indexed_paths(databases, native)
            if not paths:
                raise ValueError(f"missing native transcript for child {native}; restore it or pass --session and --transcript")
            children = indexed_children(databases, native)
            for path in sorted(paths):
                records = read_records(path)
                meta = metadata(records)
                if meta.get("id") != native:
                    raise ValueError(f"native transcript identity differs for {native}")
                if parent is None:
                    records = claim_history.root_records(path, "codex", native, records)
                else:
                    if (
                        claim_history.source_parent(meta.get("source", {})) != parent
                        or meta.get("session_id", root_identity) != root_identity
                    ):
                        raise ValueError(f"native child {native} does not belong to {root_id}")
                    records = claim_history.child_records(records, "codex", native, {"parent": parent, "ancestors": ancestors})
                for claim_session in {root_id, root_identity, native}:
                    expanded.append(entry(claim_session, path, records, native))
                for child in codex_children(records):
                    children.setdefault(child, set()).update(indexed_paths(databases, child))
            for child, paths in children.items():
                if child not in supplied and paths:
                    supplied[child] = paths
                pending.append((child, native, (*ancestors, native)))
    return expanded


def claude_agent(records: list[dict]) -> str | None:
    return next((row["agentId"] for row in records if isinstance(row.get("agentId"), str)), None)


def claude_children(records: list[dict]) -> set[str]:
    return {
        result["agentId"]
        for row in records
        if isinstance(result := row.get("toolUseResult"), dict) and isinstance(result.get("agentId"), str)
    }


def native_output(path: Path, directory: Path, session: str, identifier: str) -> dict:
    if path.parent.name != directory.name:
        raise ValueError("native tool output has an invalid artifact directory")
    if path.parent.parent.name not in {session, directory.parent.name}:
        return {"foreign_native_output": identifier}
    candidate = directory / path.name if directory.name == "tool-results" else path
    candidate = candidate.resolve()
    if candidate.parent != (directory if directory.name == "tool-results" else path.parent).resolve():
        raise ValueError("native tool output escapes its session artifact directory")
    return {"native_tool_output": candidate.read_text(encoding="utf-8"), "tool_use_id": identifier}


def persisted_output(records: list[dict], directory: Path, session: str, wanted: str) -> list[dict]:
    output = []
    background = {}
    for row in records:
        result = row.get("toolUseResult") or {}
        content = (row.get("message") or {}).get("content", [])
        blocks = (
            [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]
            if isinstance(content, list)
            else []
        )
        paths = set()
        for block in blocks:
            identifier = str(block.get("tool_use_id", ""))
            if isinstance(result, dict) and isinstance(result.get("backgroundTaskId"), str):
                background[result["backgroundTaskId"]] = identifier
            if identifier != wanted:
                continue
            if isinstance(result, dict) and isinstance(result.get("persistedOutputPath"), str):
                if len(blocks) != 1:
                    raise ValueError("claim artifact advertisement has several tool results")
                paths.add((result["persistedOutputPath"], identifier))
            text = block.get("content")
            if isinstance(text, str) and text.startswith("<persisted-output>\n"):
                match = re.search(r"Full output saved to: ([^\n]+)", text)
                if match:
                    paths.add((match[1], identifier))
        for path, identifier in sorted(paths):
            output.append(native_output(Path(path).expanduser(), directory, session, identifier))
        origin = row.get("origin") or {}
        if origin.get("kind") != "task-notification" or origin.get("producer") != "session-task" or not isinstance(content, str):
            continue
        fields = dict(re.findall(r"<(task-id|tool-use-id|output-file)>([^<]+)</\1>", content))
        identifier = fields.get("tool-use-id")
        if identifier == wanted and background.get(fields.get("task-id")) == identifier and fields.get("output-file"):
            path = Path(fields["output-file"]).expanduser()
            output.append(native_output(path, directory.with_name("tasks"), session, identifier))
    return output


def claude_entries(root: dict, supplied: dict[str, set[Path]]) -> list[dict]:
    native, root_path = root["session_id"], Path(root["transcript"]).resolve()
    directory = root_path.with_suffix("") / "subagents"
    pending = [(candidate.resolve(), candidate.stem.removeprefix("agent-")) for candidate in directory.glob("agent-*.jsonl")]
    pending.append((root_path, None))
    visited = set()
    expanded = []
    while pending:
        path, agent = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        if len(visited) >= 4096:
            raise ValueError("native session lineage exceeds its coverage bound")
        records = read_records(path)
        own = [row for row in records if row.get("sessionId") == native]
        if not own:
            if agent is not None and any(row.get("sessionId") for row in records):
                continue
            raise ValueError(f"native transcript does not belong to {native}")
        if agent is not None and claude_agent(own) != agent:
            raise ValueError(f"native subagent identity differs for {agent}")
        expanded.append(
            {**entry(native, path, own, native), "native_output_directory": str(root_path.with_suffix("") / "tool-results")}
        )
        for child in claude_children(own):
            paths = supplied.get(child) or {directory / f"agent-{child}.jsonl"}
            if not any(path.exists() for path in paths):
                foreign = list(root_path.parent.glob(f"*/subagents/agent-{child}.jsonl"))
                if foreign and all(not any(row.get("sessionId") == native for row in read_records(path)) for path in foreign):
                    continue
                raise ValueError(f"missing native subagent {child}; restore it or pass its --transcript under session {native}")
            pending.extend((path.resolve(), child) for path in paths)
    return expanded


def expand(entries: list[dict]) -> list[dict]:
    supplied: dict[str, set[Path]] = {}
    parents = {}
    agents = {}
    for item in entries:
        path = Path(item["transcript"]).resolve()
        records = read_records(path)
        meta = metadata(records)
        agent = claude_agent(records)
        native = meta.get("id") or agent or item["session_id"]
        supplied.setdefault(native, set()).add(path)
        source = meta.get("source", {})
        parents[path] = claim_history.source_parent(source) if isinstance(source, dict) else None
        agents[path] = agent
    expanded = []
    remaining = list(entries)
    while remaining:
        roots = [
            item
            for item in remaining
            if not (
                parents[Path(item["transcript"]).resolve()] in supplied
                or (
                    agents[Path(item["transcript"]).resolve()]
                    and any(
                        other["session_id"] == item["session_id"] and not agents[Path(other["transcript"]).resolve()]
                        for other in remaining
                    )
                )
            )
        ]
        if not roots:
            raise ValueError("supplied native transcript lineage has no root")
        item = roots[0]
        records = read_records(Path(item["transcript"]))
        children = codex_entries(item, supplied) if metadata(records) else claude_entries(item, supplied)
        expanded.extend(children)
        covered = {child["transcript"] for child in children}
        remaining = [candidate for candidate in remaining if str(Path(candidate["transcript"]).resolve()) not in covered]
    return expanded


def claim_target(value: dict, session: str) -> tuple[str, int] | None:
    source = value.get("claim")
    if not isinstance(source, dict) or source.get("session") != session:
        return None
    receipt = value.get("claim_comment") or {}
    record = value.get("issue") or receipt
    if not isinstance(record, dict) or not isinstance(receipt, dict):
        raise ValueError("recorded claim result has an invalid target")
    if record.get("repo") and record.get("number"):
        return claim_log.target({"session": session, "repository": record["repo"], "record": record["number"]}, session)
    if receipt or "post_claim" in value.get("completed_steps", []):
        raise ValueError("recorded claim write lacks its repository and record number")
    return None


def command_text(command: object) -> str:
    if isinstance(command, str):
        return command
    if isinstance(command, list) and all(isinstance(argument, str) for argument in command):
        return shlex.join(command)
    raise ValueError("native shell command is neither text nor argv")


def execution_item(row: dict) -> dict:
    payload = row.get("payload") or {}
    item = payload.get("item") if isinstance(payload, dict) else None
    return item if isinstance(item, dict) and item.get("type") == "CommandExecution" else {}


def tool_inputs(records: list[dict]) -> Iterator[tuple[str, str, bool]]:
    for row in records:
        item = execution_item(row)
        if item and "command" in item:
            yield "command:" + str(item.get("id", "")), json.dumps({"command": item["command"]}), False
        payload = row.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "local_shell_call":
            yield str(payload.get("call_id", "")), json.dumps(payload.get("action") or {}), False
        if isinstance(payload, dict) and payload.get("type") in {"function_call", "custom_tool_call"}:
            name = str(payload.get("name", "")).split(".")[-1].casefold()
            if name in {"exec", "exec_command", "bash", "write_stdin", "shell_command", "shell"}:
                raw = payload.get("arguments", payload.get("input", ""))
                yield (
                    str(payload.get("call_id", "")),
                    raw if isinstance(raw, str) else json.dumps(raw),
                    name == "exec" and payload["type"] == "custom_tool_call",
                )
        content = (row.get("message") or {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
                    command = (block.get("input") or {}).get("command")
                    if isinstance(command, str):
                        yield str(block.get("id", "")), command, False


def denied_invocations(records: list[dict]) -> set[str]:
    denied = set()
    for row in records:
        item = execution_item(row)
        if item.get("status") == "declined":
            denied.add("command:" + str(item.get("id", "")))
        content = (row.get("message") or {}).get("content", [])
        if row.get("toolDenialKind") and isinstance(row["toolDenialKind"], str) and isinstance(content, list):
            denied.update(
                str(block.get("tool_use_id", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            )
    return denied


def tool_outputs(records: list[dict]) -> dict[str, list[object]]:
    outputs: dict[str, list[object]] = {}
    for row in records:
        item = execution_item(row)
        if item:
            outputs.setdefault("command:" + str(item.get("id", "")), []).append(item.get("aggregated_output"))
        if "native_tool_output" in row:
            outputs.setdefault(str(row.get("tool_use_id", "")), []).append(row["native_tool_output"])
        if "foreign_native_output" in row:
            outputs.setdefault(str(row["foreign_native_output"]), []).append({"foreign_native_output": True})
        payload = row.get("payload")
        if isinstance(payload, dict) and payload.get("type") in {
            "function_call_output",
            "custom_tool_call_output",
            "local_shell_call_output",
        }:
            outputs.setdefault(str(payload.get("call_id", "")), []).append(payload.get("output"))
        content = (row.get("message") or {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    outputs.setdefault(str(block.get("tool_use_id", "")), []).append(block.get("content"))
    inputs = {identifier: text for identifier, text, _ in tool_inputs(records)}
    recorded = {identifier: list(blocks) for identifier, blocks in outputs.items()}
    for identifier, blocks in recorded.items():
        yielded = {item["session_id"] for block in blocks for item in values(block) if type(item.get("session_id")) is int}
        if not yielded:
            continue
        for followup, text in inputs.items():
            if followup != identifier and any(re.search(rf"session_id[\"']?\s*:\s*{process}(?!\d)", text) for process in yielded):
                outputs[identifier].extend(recorded.get(followup, []))
    return outputs


def command_literal(text: str, start: int, literal: str) -> str | None:
    tokens = [match[0] for match in re.finditer(literal + r"|[A-Za-z_$][A-Za-z0-9_$]*|[^\s]", text[start:])]
    if not tokens or tokens[0] != "{":
        return None
    depth = 0
    for index, token in enumerate(tokens):
        if token in {"{", "[", "("}:
            depth += 1
        elif token in {"}", "]", ")"}:
            depth -= 1
            if depth == 0:
                break
        if depth != 1 or token.strip("\"'") not in {"cmd", "command", "chars"} or tokens[index + 1 : index + 2] != [":"]:
            continue
        tail = tokens[index + 2 : index + 4]
        if len(tail) != 2 or tail[1] not in {",", "}"} or not re.fullmatch(literal, tail[0], re.DOTALL):
            return None
        value = tail[0]
        if value.startswith("`"):
            return value[1:-1] if "${" not in value else None
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return None
    return None


JAVASCRIPT_LITERAL = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
CLAIM_WORD = re.compile(r"""(?:^|[\s'"`])claim(?:$|[\s'"`])""")
JAVASCRIPT_PATCH = re.compile(r"\btools\.apply_patch\s*\(\s*(?:" + JAVASCRIPT_LITERAL + r")\s*\)", re.DOTALL)


def without_patch_text(text: str) -> str:
    return JAVASCRIPT_PATCH.sub(lambda match: match[0] if "${" in match[0] else "tools.apply_patch()", text)


def javascript_names(text: str) -> set[str]:
    names = set()
    for match in re.finditer(JAVASCRIPT_LITERAL + r"|//[^\n]*|/\*.*?\*/|(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)", text, re.DOTALL):
        if match["name"]:
            names.add(match["name"])
        elif match[0].startswith("`") and "${" in match[0]:
            names.update(re.findall(r"\b(?:store|load)\b", match[0]))
    return names


@dataclass
class ShellCommand:
    text: str
    interactive: bool = False


@dataclass
class ShellTexts:
    commands: list[ShellCommand]
    opaque: bool = False
    interactive_opaque: bool = False


def shell_texts(text: str, javascript: bool) -> ShellTexts:
    if not javascript:
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            command = decoded.get("cmd", decoded.get("command", decoded.get("chars")))
            commands = [ShellCommand(command_text(command), "chars" in decoded)] if command is not None else []
            return ShellTexts(commands)
        return ShellTexts([ShellCommand(text)])
    literal = JAVASCRIPT_LITERAL
    reference = r"\btools\s*\[\s*['\"](?:exec_command|shell_command|shell|write_stdin)['\"]\s*\]|\b(?:exec_command|shell_command|write_stdin)\b|\btools\.shell\b|\bshell\s*\("
    site = re.compile(
        r"(?P<call>\btools\.(?:exec_command|shell_command|shell|write_stdin)\s*\()|(?P<reference>"
        + reference
        + r")|(?:"
        + literal
        + r")|(?://[^\n]*|/\*.*?\*/)",
        re.DOTALL,
    )
    commands = []
    opaque = False
    interactive_opaque = False
    for match in site.finditer(text):
        if match["reference"] is not None:
            opaque = True
            interactive_opaque = interactive_opaque or "write_stdin" in match[0]
        elif match["call"] is not None:
            command = command_literal(text, match.end(), literal)
            if command is None:
                opaque = True
                interactive_opaque = interactive_opaque or "write_stdin" in match[0]
            else:
                commands.append(ShellCommand(command, "write_stdin" in match[0]))
        elif match[0].startswith("`") and "${" in match[0] and re.search(reference, match[0]):
            opaque = True
            interactive_opaque = interactive_opaque or "write_stdin" in match[0]
    return ShellTexts(commands, opaque, interactive_opaque)


SHELL_NAMES = {"sh", "bash", "zsh", "dash", "ksh", "fish"}


def has_claim_text(text: str) -> bool:
    return "gh-plan" in text and CLAIM_WORD.search(text) is not None


def is_interpreter(consumer: str) -> bool:
    return (
        Path(consumer).name in SHELL_NAMES | {"node", "nodejs", "deno", "bun", "perl", "ruby"}
        or re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(consumer).name) is not None
    )


def has_executable_claim_source(text: str, consumer: str, *, claim_related: bool = False) -> bool:
    if not claim_related and not has_claim_text(text):
        return False
    if not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(consumer).name):
        if Path(consumer).name in SHELL_NAMES | {"eval", "source", "."}:
            discovery = shell_claims(text, claim_related=claim_related)
            return bool(discovery.arguments) or discovery.opaque or discovery.decode_failed
        return is_interpreter(consumer)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return True
    pattern = re.compile(
        r"(?:run|run_path|run_module|call|check_\w+|[Pp]open\w*|system|getoutput|getstatusoutput|exec\w*|spawn\w*|posix_spawn\w*|create_subprocess_\w+|eval|shell_command)"
    )
    modules = {"subprocess", "os", "asyncio", "pty", "runpy"}
    launchers = {"exec": "exec", "eval": "eval"}
    claim_names: set[str] = set()
    binding_texts: dict[str, str] = {}
    for node in ast.walk(tree):
        for target, literal_text in python_literal_bindings(node):
            for name in python_binding_names(target):
                text = f"{binding_texts.get(name, '')} {literal_text}"
                binding_texts[name] = text
                if has_claim_text(text):
                    claim_names.add(name)
    unknown_bindings = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(item.asname or item.name for item in node.names if item.name in modules)
        if isinstance(node, ast.ImportFrom) and node.module in modules:
            launchers.update({item.asname or item.name: item.name for item in node.names if pattern.fullmatch(item.name)})
        if isinstance(node, ast.ImportFrom) and any(item.name == "*" for item in node.names):
            unknown_bindings = True
        if isinstance(node, ast.Call) and python_dynamic_import(node.func) and not python_process_module(node, modules):
            unknown_bindings = True
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "importlib"
            and any(item.name == "import_module" for item in node.names)
        ):
            unknown_bindings = True
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
            if (name := python_launcher_name(node.value, modules, launchers, pattern)) is not None:
                launchers.update({target.id: name for target in node.targets if isinstance(target, ast.Name)})
        if isinstance(node, ast.Assign) and python_process_module(node.value, modules):
            modules.update(target.id for target in node.targets if isinstance(target, ast.Name))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    direct = {id(node.func) for node in calls}
    if any(
        id(node) not in direct and python_launcher_name(node, modules, launchers, pattern) is not None for node in ast.walk(tree)
    ):
        return True
    for node in calls:
        name = python_launcher_name(node.func, modules, launchers, pattern)
        if name is None:
            if (
                not python_dynamic_import(node.func)
                and not python_data_call(node.func)
                and (unknown_bindings or python_claim_arguments(node, claim_names))
            ):
                return True
            continue
        if name == "*":
            return True
        command = (
            node.args[0] if node.args else next((item.value for item in node.keywords if item.arg in {"args", "command"}), None)
        )
        if name.startswith(("execl", "spawnl", "create_subprocess_exec")):
            command = ast.Tuple(elts=node.args, ctx=ast.Load())
        elif name.startswith(("execv", "spawnv", "posix_spawn")) and len(node.args) > 1:
            command = node.args[2] if name.startswith("spawnv") and len(node.args) > 2 else node.args[1]
        if not name.startswith(("execv", "spawnv", "posix_spawn")) and python_data_argv(node, command):
            continue
        try:
            value = ast.literal_eval(command)
        except (ValueError, TypeError):
            return True
        if (
            isinstance(value, (list, tuple))
            and all(isinstance(argument, str) for argument in value)
            and accepts_claim_data(list(value))
        ):
            continue
        if "gh-plan" in str(value) and (CLAIM_WORD.search(str(value)) or name in {"run_path", "run_module"}):
            return True
    return False


NON_EXECUTING_COMMANDS = frozenset({"cat", "cut", "echo", "head", "ls", "nl", "printf", "tail", "tr", "uniq", "wc", "zstd"})


def python_data_argv(call: ast.Call, command: ast.AST | None) -> bool:
    return (
        not any(item.arg in {None, "executable", "shell"} for item in call.keywords)
        and (len(call.args) <= 1 or command is not call.args[0])
        and isinstance(command, (ast.List, ast.Tuple))
        and bool(command.elts)
        and isinstance(executable := command.elts[0], ast.Constant)
        and isinstance(executable.value, str)
        and Path(executable.value).name in NON_EXECUTING_COMMANDS
    )


def python_dynamic_import(function: ast.AST) -> bool:
    return (isinstance(function, ast.Name) and function.id == "__import__") or (
        isinstance(function, ast.Attribute) and function.attr == "import_module"
    )


def python_process_module(node: ast.AST, modules: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in modules
    return (
        isinstance(node, ast.Call)
        and python_dynamic_import(node.func)
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value in modules
    )


def python_data_call(function: ast.AST) -> bool:
    if isinstance(function, ast.Name):
        return function.id in {
            "print",
            "repr",
            "str",
            "bytes",
            "bool",
            "int",
            "float",
            "complex",
            "dict",
            "list",
            "tuple",
            "set",
            "frozenset",
            "Path",
            "len",
            "sorted",
            "reversed",
            "enumerate",
            "zip",
            "range",
            "all",
            "any",
            "sum",
            "min",
            "max",
        }
    return isinstance(function, ast.Attribute) and (
        function.attr.startswith("assert")
        or function.attr
        in {
            "read_text",
            "read_bytes",
            "write",
            "writelines",
            "append",
            "extend",
            "add",
            "search",
            "match",
            "fullmatch",
            "findall",
            "compile",
            "startswith",
            "endswith",
            "count",
            "dedent",
            "debug",
            "info",
            "warning",
            "error",
            "exception",
            "critical",
            "log",
            "write_text",
            "write_bytes",
            "replace",
            "join",
            "index",
            "find",
            "split",
            "splitlines",
            "strip",
            "lstrip",
            "rstrip",
            "encode",
            "decode",
            "format",
            "dumps",
            "dump",
            "loads",
            "load",
            "parse",
            "shlex",
            "literal_eval",
            "literal_targets",
            "shell_claims",
            "shell_groups",
            "has_executable_claim_source",
        }
    )


def python_literal_bindings(node: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value is not None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            yield target, python_literal_text(node.value)
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        yield node.target, python_literal_text(node.iter)
    elif isinstance(node, ast.withitem) and node.optional_vars is not None:
        yield node.optional_vars, python_literal_text(node.context_expr)
    elif isinstance(node, ast.Match):
        for case in node.cases:
            yield case.pattern, python_literal_text(node.subject)


def python_binding_names(target: ast.AST) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            yield from python_binding_names(item)
    elif isinstance(target, (ast.Starred, ast.Attribute, ast.Subscript)):
        yield from python_binding_names(target.value)
    elif isinstance(target, ast.pattern):
        for pattern in ast.walk(target):
            if isinstance(pattern, (ast.MatchAs, ast.MatchStar)) and pattern.name is not None:
                yield pattern.name
            elif isinstance(pattern, ast.MatchMapping) and pattern.rest is not None:
                yield pattern.rest


def python_literal_text(node: ast.AST) -> str:
    return " ".join(item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str))


def python_claim_text(node: ast.AST) -> bool:
    return has_claim_text(python_literal_text(node))


def python_claim_arguments(call: ast.Call, claim_names: set[str]) -> bool:
    arguments = [*call.args, *(item.value for item in call.keywords)]
    return any(
        not python_parsed_root(argument)
        and (
            python_claim_text(argument) or any(isinstance(node, ast.Name) and node.id in claim_names for node in ast.walk(argument))
        )
        for argument in arguments
    )


def python_parsed_root(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "root_node"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "parse"
    )


def python_launcher_name(node: ast.AST, modules: set[str], launchers: dict[str, str], pattern: re.Pattern) -> str | None:
    if isinstance(node, ast.Name):
        return launchers.get(node.id) if isinstance(node.ctx, ast.Load) else None
    if isinstance(node, ast.Attribute) and pattern.fullmatch(node.attr):
        root = node.value
        while isinstance(root, ast.Attribute):
            root = root.value
        return node.attr if python_process_module(root, modules) else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
        receiver, member = node.args[:2]
        if python_process_module(receiver, modules):
            if not isinstance(member, ast.Constant):
                return "*"
            if isinstance(member.value, str) and pattern.fullmatch(member.value):
                return member.value
    return None


@dataclass
class ShellClaims:
    arguments: list[list[str]] = field(default_factory=list)
    opaque: bool = False
    receipt_pure: bool = True
    decode_failed: bool = False


@dataclass
class ProgramSource:
    text: str
    consumer: str
    generated: bool = False
    produced: bool = False
    argument_indexes: tuple[int, ...] = ()


def shell_nodes(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from shell_nodes(child)


def shell_generated(node: Node) -> bool:
    return any(
        child.type in {"simple_expansion", "expansion", "command_substitution", "process_substitution", "arithmetic_expansion"}
        for child in shell_nodes(node)
    )


def shell_word(node: Node) -> str:
    text = node.text.decode()
    try:
        words = shlex.split(text.replace("\\\n", ""))
    except ValueError:
        return text
    return words[0] if len(words) == 1 else text


def command_words(node: Node) -> list[Node]:
    name = node.child_by_field_name("name")
    words = ([name] if name is not None else []) + node.children_by_field_name("argument")
    parent = node.parent
    if parent is not None and parent.type == "redirected_statement" and parent.child_by_field_name("body") == node:
        for redirect in parent.children_by_field_name("redirect"):
            if words and words[-1].type == "number" and words[-1].end_byte == redirect.start_byte:
                words.pop()
            if redirect.type == "file_redirect":
                words.extend(redirect.children_by_field_name("destination")[1:])
    return sorted(words, key=lambda word: word.start_byte)


def command_argv(node: Node) -> list[str]:
    return [shell_word(word) for word in command_words(node)]


@dataclass
class CommandPosition:
    index: int
    python_script: bool = False


def command_position(argv: list[str]) -> CommandPosition | None:
    if not argv:
        return None
    if Path(argv[0]).name != "uv":
        return CommandPosition(0)
    if argv[1:2] != ["run"]:
        return None
    value_flags = {
        "--python",
        "-p",
        "--with",
        "-w",
        "--with-requirements",
        "--directory",
        "--project",
        "--env-file",
        "--extra",
        "--group",
        "--package",
        "--config-file",
    }
    boolean_flags = {
        "--no-project",
        "--no-config",
        "--offline",
        "--no-cache",
        "--frozen",
        "--locked",
        "--no-sync",
        "--isolated",
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "--no-progress",
        "--no-env-file",
        "--no-dev",
        "--all-extras",
        "--all-groups",
        "--active",
    }
    index = 2
    python_script = False
    while index < len(argv) and argv[index].startswith("-"):
        flag, equal, value = argv[index].partition("=")
        if flag == "--" and not equal:
            index += 1
            break
        if flag in value_flags:
            if equal:
                if not value:
                    return None
                index += 1
                continue
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                return None
            index += 2
            continue
        if flag == "--script" and not equal:
            python_script = True
        elif flag not in boolean_flags or equal:
            if argv[index] == "-" and python_script:
                break
            return None
        index += 1
    return CommandPosition(index, python_script) if index < len(argv) or python_script else None


def script_consumer(argv: list[str]) -> str:
    position = command_position(argv)
    if position is None:
        return ""
    if position.python_script:
        return "python"
    command = argv[position.index]
    return command if is_interpreter(command) or command in {"eval", "source", "."} else ""


def interpreter_arguments(argv: list[str]) -> list[str]:
    position = command_position(argv)
    return argv[position.index + (not position.python_script) :] if position is not None else []


def reads_program_stdin(argv: list[str], consumer: str) -> bool:
    if not is_interpreter(consumer):
        return False
    arguments = interpreter_arguments(argv)
    return not any(is_evaluator_flag(argument, consumer) for argument in arguments) and (
        any(argument in {"-", "-s", "/dev/stdin"} for argument in arguments)
        or any(re.fullmatch(r"-[a-z]*s[a-z]*", argument) for argument in arguments)
        or all(argument.startswith("-") for argument in arguments)
    )


DATA_COMMANDS = frozenset(
    {
        "rg",
        "grep",
        "git",
        "gh",
        "cat",
        "tee",
        "echo",
        "printf",
        "sed",
        "jq",
        "read",
        "head",
        "tail",
        "cut",
        "wc",
        "sort",
        "uniq",
        "tr",
        "nl",
        "column",
        "less",
        "pbcopy",
        "diff",
        "cd",
        "pushd",
        "popd",
        "pwd",
        "set",
        "ps",
        "kill",
        "ls",
        "mkdir",
        "test",
        "[",
        "true",
        "false",
        "pytest",
        "gh-issue",
        "gh-comment",
    }
)
SAFE_GIT_CONFIG = frozenset(
    {
        "user.name",
        "user.email",
        "commit.gpgsign",
        "tag.gpgsign",
        "core.quotepath",
        "core.autocrlf",
        "core.filemode",
        "init.defaultbranch",
        "safe.directory",
        "pull.rebase",
        "pull.ff",
        "push.default",
        "push.autosetupremote",
        "merge.conflictstyle",
        "rebase.autosquash",
        "rebase.autostash",
        "log.showsignature",
        "status.showuntrackedfiles",
    }
)
GIT_GLOBAL_VALUE_OPTIONS = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})
GIT_GLOBAL_FLAGS = frozenset({"--version", "--help", "--no-pager", "-P", "--no-optional-locks", "--bare", "--literal-pathspecs", "--no-replace-objects"})
GIT_DATA_COMMANDS = frozenset(
    {
        "add",
        "am",
        "apply",
        "bisect",
        "blame",
        "branch",
        "cat-file",
        "check-ignore",
        "checkout",
        "cherry",
        "cherry-pick",
        "clean",
        "clone",
        "commit",
        "config",
        "count-objects",
        "describe",
        "diff",
        "diff-files",
        "diff-index",
        "diff-tree",
        "fetch",
        "for-each-ref",
        "format-patch",
        "fsck",
        "gc",
        "grep",
        "hash-object",
        "init",
        "log",
        "ls-files",
        "ls-remote",
        "ls-tree",
        "merge",
        "merge-base",
        "mv",
        "name-rev",
        "notes",
        "pull",
        "push",
        "range-diff",
        "rebase",
        "reflog",
        "remote",
        "reset",
        "restore",
        "rev-list",
        "rev-parse",
        "revert",
        "rm",
        "shortlog",
        "show",
        "show-ref",
        "sparse-checkout",
        "stash",
        "status",
        "submodule",
        "switch",
        "symbolic-ref",
        "tag",
        "update-index",
        "update-ref",
        "var",
        "version",
        "worktree",
    }
)
GIT_EXECUTING_OPTIONS = {
    "rebase": ("--exec", "-x"),
    "grep": ("--open-files-in-pager", "-O"),
    "clone": ("--upload-pack", "-u", "--config", "-c"),
    "fetch": ("--upload-pack",),
    "pull": ("--upload-pack",),
    "ls-remote": ("--upload-pack", "-u"),
    "push": ("--receive-pack", "--exec"),
}
GIT_EXECUTING_ACTIONS = {"bisect": "run", "submodule": "foreach"}
GIT_VALUE_OPTIONS = frozenset({"-e", "--regexp", "-f", "--file", "-m", "--message", "-F", "-C", "-c", "--type", "--default"})
GIT_CONFIG_READ_OPTIONS = frozenset(
    {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list", "--unset", "--unset-all", "--remove-section", "--rename-section"}
)
GH_DATA_COMMANDS = frozenset(
    {
        "api",
        "auth",
        "browse",
        "cache",
        "co",
        "completion",
        "gist",
        "help",
        "issue",
        "label",
        "org",
        "pr",
        "project",
        "release",
        "repo",
        "ruleset",
        "run",
        "search",
        "secret",
        "ssh-key",
        "gpg-key",
        "status",
        "variable",
        "version",
        "workflow",
        "alias",
        "config",
        "extension",
        "ext",
        "extensions",
    }
)
GH_VALUE_OPTIONS = frozenset({"-R", "--repo", "-h", "--host", "--hostname"})
SED_LONG_OPTIONS = (
    "--debug",
    "--expression",
    "--file",
    "--follow-symlinks",
    "--help",
    "--in-place",
    "--line-length",
    "--null-data",
    "--posix",
    "--quiet",
    "--regexp-extended",
    "--sandbox",
    "--separate",
    "--silent",
    "--unbuffered",
    "--version",
    "--zero-terminated",
)
SEARCH_VALUE_OPTIONS = frozenset({"-e", "--regexp", "-f", "--file", "-g", "--glob", "-t", "--type", "-T", "--type-not"})
EXECUTING_ENVIRONMENT = re.compile(
    r"GIT_(?:PAGER|EDITOR|SEQUENCE_EDITOR|EXTERNAL_DIFF|SSH|SSH_COMMAND|ASKPASS|CONFIG\w*|EXEC_PATH|PROXY_COMMAND|TEMPLATE_DIR)"
    r"|GH_(?:PAGER|EDITOR|BROWSER|CONFIG_DIR)|PAGER|EDITOR|VISUAL|BROWSER|LESS\w*|PYTEST_\w+|PYTHONPATH|RIPGREP_CONFIG_PATH"
    r"|BASH_ENV|ENV"
)
SORT_VALUE_OPTIONS = frozenset({"-o", "--output", "-k", "--key", "-t", "-T", "-S"})
INERT_ENVIRONMENT_VALUES = frozenset({"", "true", ":", "cat", "less", "0", "1"})


def long_option(argument: str, option: str, options: tuple[str, ...] = ()) -> bool:
    flag = argument.partition("=")[0]
    if flag == option:
        return True
    return (
        len(flag) > 3
        and option.startswith(flag)
        and not any(other != option and other.startswith(flag) for other in options)
    )


def short_option(argument: str, letter: str) -> bool:
    return re.match(rf"-[A-Za-z0-9]*{letter}", argument) is not None and not argument.startswith("--")


def option_words(arguments: list[str], value_options: frozenset[str]) -> Iterator[str]:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return
        if argument.startswith("-"):
            yield argument
            index += 2 if argument in value_options else 1
            continue
        index += 1


def operands(arguments: list[str], value_options: frozenset[str]) -> list[str]:
    words: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return words + arguments[index + 1 :]
        if argument.startswith("-") and argument != "-":
            index += 2 if argument in value_options else 1
            continue
        words.append(argument)
        index += 1
    return words


def safe_git_config(setting: str, value: str | None = None) -> bool:
    key, equal, assigned = setting.partition("=")
    key = key.lower()
    value = assigned if equal else value
    return (
        key in SAFE_GIT_CONFIG
        or key.startswith(("advice.", "color."))
        or (key == "core.hookspath" and value == "/dev/null")
    )


def git_config_is_data(arguments: list[str]) -> bool:
    if any(argument in {"-e", "--edit"} for argument in arguments):
        return False
    if any(argument.partition("=")[0] in GIT_CONFIG_READ_OPTIONS for argument in arguments):
        return True
    words = operands(arguments, frozenset({"-f", "--file", "--blob", "--type", "--default", "--comment"}))
    if words[:1] in (["get"], ["list"], ["unset"], ["remove-section"], ["rename-section"]):
        return True
    if words[:1] == ["edit"]:
        return False
    if words[:1] == ["set"]:
        words = words[1:]
    return len(words) < 2 or safe_git_config(words[0], words[1])


def git_is_data(arguments: list[str]) -> bool:
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        argument = arguments[index]
        if argument == "-c":
            if index + 1 >= len(arguments) or not safe_git_config(arguments[index + 1]):
                return False
            index += 2
        elif argument in GIT_GLOBAL_VALUE_OPTIONS:
            index += 2
        elif argument in GIT_GLOBAL_FLAGS or argument.partition("=")[0] in GIT_GLOBAL_VALUE_OPTIONS:
            index += 1
        else:
            return False
    if index >= len(arguments):
        return True
    command, rest = arguments[index], arguments[index + 1 :]
    if command not in GIT_DATA_COMMANDS:
        return False
    if command == "config":
        return git_config_is_data(rest)
    if command in GIT_EXECUTING_ACTIONS:
        return GIT_EXECUTING_ACTIONS[command] not in operands(rest, GIT_VALUE_OPTIONS)
    return not any(
        long_option(argument, option) if option.startswith("--") else short_option(argument, option[1])
        for option in GIT_EXECUTING_OPTIONS.get(command, ())
        for argument in option_words(rest, GIT_VALUE_OPTIONS)
    )


def gh_is_data(arguments: list[str]) -> bool:
    words = operands(arguments, GH_VALUE_OPTIONS)
    if not words:
        return True
    command, action = words[0], words[1] if len(words) > 1 else ""
    if command not in GH_DATA_COMMANDS:
        return False
    if command == "alias":
        return action not in {"set", "import"}
    if command in {"ext", "extension", "extensions"}:
        return action != "exec"
    if command == "config":
        return action != "set" or words[2:3] not in (["editor"], ["pager"], ["browser"])
    if command == "repo":
        return action != "clone" or "--" not in arguments
    return True


def sed_scripts(arguments: list[str]) -> list[str] | None:
    scripts: list[str] = []
    positionals: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            positionals.extend(arguments[index:])
            break
        if argument.startswith("--"):
            if long_option(argument, "--sandbox", SED_LONG_OPTIONS):
                return []
            if long_option(argument, "--file", SED_LONG_OPTIONS):
                return None
            if long_option(argument, "--expression", SED_LONG_OPTIONS):
                if "=" in argument:
                    scripts.append(argument.partition("=")[2])
                elif index < len(arguments):
                    scripts.append(arguments[index])
                    index += 1
                else:
                    return None
            elif long_option(argument, "--line-length", SED_LONG_OPTIONS) and "=" not in argument:
                index += 1
            elif not any(long_option(argument, option, SED_LONG_OPTIONS) for option in SED_LONG_OPTIONS):
                return None
            continue
        if not argument.startswith("-") or argument == "-":
            positionals.append(argument)
            continue
        for offset, letter in enumerate(argument[1:], 2):
            if letter == "f":
                return None
            if letter == "i":
                break
            if letter in "el":
                value = argument[offset:]
                if not value:
                    if index >= len(arguments):
                        return None
                    value = arguments[index]
                    index += 1
                if letter == "e":
                    scripts.append(value)
                break
            if letter not in {"n", "r", "E", "s", "u", "z"}:
                return None
    if not scripts:
        if not positionals:
            return None
        scripts.append(positionals[0])
    return scripts


def sed_bracket_end(script: str, index: int) -> int | None:
    index += 1
    if index < len(script) and script[index] == "^":
        index += 1
    if index < len(script) and script[index] == "]":
        index += 1
    while index < len(script) and script[index] != "]":
        if script[index] == "[" and index + 1 < len(script) and script[index + 1] in ":.=":
            close = script.find(script[index + 1] + "]", index + 2)
            if close < 0:
                return None
            index = close + 2
            continue
        index += 1
    return index + 1 if index < len(script) else None


def sed_delimited(script: str, index: int, delimiter: str, regex_parts: int, parts: int) -> int | None:
    for part in range(parts):
        while index < len(script) and script[index] != delimiter:
            if script[index] == "\\":
                index += 2
                continue
            if script[index] == "\n":
                return None
            if part < regex_parts and script[index] == "[":
                end = sed_bracket_end(script, index)
                if end is None:
                    return None
                index = end
                continue
            index += 1
        if index >= len(script):
            return None
        index += 1
    return index


def sed_line_end(script: str, index: int) -> int:
    while index < len(script) and script[index] != "\n":
        index += 2 if script[index] == "\\" else 1
    return index


def sed_address(script: str, index: int) -> int | None:
    while index < len(script):
        character = script[index]
        if character.isdigit() or character in "$,~+ \t!":
            index += 1
        elif character in "/\\":
            if character == "\\":
                if index + 1 >= len(script):
                    return None
                index += 1
            end = sed_delimited(script, index + 1, script[index], 1, 1)
            if end is None:
                return None
            index = end
            while index < len(script) and script[index] in "IM":
                index += 1
        else:
            return index
    return index


def sed_executes(script: str) -> bool:
    index = 0
    while index < len(script):
        if script[index] in " \t\n;{}":
            index += 1
            continue
        address_end = sed_address(script, index)
        if address_end is None:
            return True
        if address_end >= len(script):
            return False
        index = address_end
        command = script[index]
        index += 1
        if command in {"{", "}"}:
            continue
        if command == "e":
            return True
        if command in {"s", "y"}:
            if index >= len(script):
                return True
            end = sed_delimited(script, index + 1, script[index], 1 if command == "s" else 0, 2)
            if end is None:
                return True
            index = end
            if command == "s":
                while index < len(script) and script[index] not in " \t\n;}":
                    if script[index] == "e":
                        return True
                    if script[index] in {"w", "W"}:
                        index = sed_line_end(script, index)
                        break
                    if not (script[index].isdigit() or script[index] in {"g", "p", "i", "I", "m", "M"}):
                        return True
                    index += 1
            continue
        if command in {"a", "i", "c", "r", "R", "w", "W", "#"}:
            index = sed_line_end(script, index)
        elif command in {":", "b", "t", "T", "v", "l", "L", "q", "Q"}:
            while index < len(script) and script[index] not in "\n;}":
                index += 1
        elif command not in {"=", "d", "D", "g", "G", "h", "H", "n", "N", "p", "P", "x", "z", "F"}:
            return True
    return False


def sed_is_data(arguments: list[str]) -> bool:
    scripts = sed_scripts(arguments)
    return scripts is not None and not any(sed_executes(script) for script in scripts)


def less_is_data(arguments: list[str]) -> bool:
    options = arguments[: arguments.index("--")] if "--" in arguments else arguments
    return not any(
        re.match(r"\+\+?\d*[!|]", argument) is not None
        or argument.startswith("--lesskey")
        or re.match(r"-[^-+]*k", argument) is not None
        for argument in options
    )


def subscript_is_data(arguments: list[str], name: str) -> bool:
    if name == "read":
        return not any("[" in argument for argument in arguments)
    if name == "printf":
        return not any(
            argument.startswith("-v") and "[" in (argument[2:] or (arguments[index + 1] if index + 1 < len(arguments) else ""))
            for index, argument in enumerate(arguments)
        )
    return not any(
        argument in {"-v", "-R"} and index + 1 < len(arguments) and "[" in arguments[index + 1]
        for index, argument in enumerate(arguments)
    )


def data_command_executes(argv: list[str]) -> bool:
    position = command_position(argv)
    if position is None or position.index >= len(argv):
        return False
    name = Path(argv[position.index]).name
    arguments = argv[position.index + 1 :]
    if name == "git":
        return not git_is_data(arguments)
    if name == "gh":
        return not gh_is_data(arguments)
    if name == "sed":
        return not sed_is_data(arguments)
    if name == "rg":
        return any(argument.partition("=")[0] == "--pre" for argument in option_words(arguments, SEARCH_VALUE_OPTIONS))
    if name == "sort":
        return any(long_option(argument, "--compress-program") for argument in option_words(arguments, SORT_VALUE_OPTIONS))
    if name == "less":
        return not less_is_data(arguments)
    if name in {"read", "printf", "test", "["}:
        return not subscript_is_data(arguments, name)
    return False


def loads_test_code(argv: list[str]) -> bool:
    position = command_position(argv)
    return position is not None and position.index < len(argv) and Path(argv[position.index]).name == "pytest"


def claim_text_outside(root: Node, node: Node) -> bool:
    source = root.text or b""
    return has_claim_text((source[: node.start_byte] + source[node.end_byte :]).decode(errors="replace"))


def environment_executes(assignment: Node) -> bool:
    name = assignment.child_by_field_name("name")
    value = assignment.child_by_field_name("value")
    return (
        name is not None
        and EXECUTING_ENVIRONMENT.fullmatch(name.text.decode()) is not None
        and (shell_word(value) if value is not None else "") not in INERT_ENVIRONMENT_VALUES
    )


def accepts_claim_data(argv: list[str]) -> bool:
    position = command_position(argv)
    if position is None:
        return False
    name = Path(argv[position.index]).name if position.index < len(argv) else ""
    if name in DATA_COMMANDS:
        return not data_command_executes(argv)
    if name.endswith(".py") and name != "gh-plan.py":
        return True
    consumer = script_consumer(argv)
    arguments = interpreter_arguments(argv)
    if (
        not is_interpreter(consumer)
        or reads_program_stdin(argv, consumer)
        or any(is_evaluator_flag(argument, consumer) for argument in arguments)
    ):
        return False
    script = arguments[0] if arguments and not arguments[0].startswith("-") else ""
    return bool(re.fullmatch(r"[A-Za-z0-9_./@:+~-]+", script)) and ("/" in script or bool(Path(script).suffix))


def input_consumer(argv: list[str]) -> str | None:
    if accepts_claim_data(argv):
        return None
    consumer = script_consumer(argv)
    if consumer in {"source", "."} and interpreter_arguments(argv)[:1] in (["/dev/stdin"], ["/dev/fd/0"], ["-"]):
        return "bash"
    if reads_program_stdin(argv, consumer):
        return consumer
    if Path(consumer).name in SHELL_NAMES:
        return ""
    if is_interpreter(consumer) and not any(is_evaluator_flag(argument, consumer) for argument in interpreter_arguments(argv)):
        return consumer
    if consumer:
        return None
    return ""


def statement_command(node: Node) -> Node | None:
    if node.type == "command":
        return node
    if node.type == "list":
        return statement_command(node.named_children[-1])
    body = node.child_by_field_name("body")
    return statement_command(body) if body is not None else None


def node_program(node: Node, consumer: str, *, produced: bool = False) -> ProgramSource:
    text = node.text.decode() if produced or node.type == "heredoc_body" else shell_word(node)
    return ProgramSource(text, consumer, shell_generated(node), produced)


def shell_evaluator_payload(argv: list[str], start: int) -> int | None:
    operand_count = sum(letter in "oO" for letter in argv[start - 1][1:-1])
    if any(argument.startswith(("-", "+")) for argument in argv[start : start + operand_count]):
        return None
    start += operand_count
    index = start
    while index < len(argv) and argv[index].startswith(("-", "+")):
        option = argv[index]
        if option == "--":
            return index + 1 if index + 1 < len(argv) else None
        if re.fullmatch(r"[+-][A-Za-z]*[oO]", option) or option in {"--rcfile", "--init-file"}:
            if index + 1 >= len(argv) or argv[index + 1].startswith(("-", "+")):
                return None
            index += 2
            continue
        if option in {"--norc", "--noprofile", "--posix", "--login"}:
            index += 1
            continue
        if re.fullmatch(r"[+-][A-Za-z]+", option) is None or any(letter in option[1:] for letter in "oO"):
            return None
        index += 1
    return index if index < len(argv) else None


def command_programs(node: Node) -> Iterator[ProgramSource]:
    words = command_words(node)
    argv = [shell_word(word) for word in words]
    if not argv or accepts_claim_data(argv):
        return
    position = command_position(argv)
    if position is None:
        return
    consumer = script_consumer(argv)
    offset = position.index + (not position.python_script) if consumer else len(argv)
    if consumer == "eval":
        yield ProgramSource(
            " ".join(argv[offset:]),
            consumer,
            any(shell_generated(word) for word in words[offset:]),
            argument_indexes=tuple(range(offset, len(argv))),
        )
        return
    for index in range(offset, len(argv) - 1):
        argument = argv[index]
        if is_evaluator_flag(argument, consumer):
            payload = shell_evaluator_payload(argv, index + 1) if Path(consumer).name in SHELL_NAMES else index + 1
            if payload is None:
                yield ProgramSource("", consumer, generated=True)
                return
            program = node_program(words[payload], consumer)
            program.argument_indexes = tuple(range(offset, payload + 1))
            yield program
            return
    script = next((word for word, value in zip(words[offset:], argv[offset:], strict=True) if not value.startswith("-")), None)
    if script is not None and consumer in {"source", "."}:
        program = node_program(script, consumer, produced=True)
        program.argument_indexes = (words.index(script),)
        yield program


def input_programs(node: Node) -> Iterator[ProgramSource]:
    if node.type == "pipeline":
        stages = node.named_children
        for index, stage in enumerate(stages[1:], 1):
            command = statement_command(stage)
            argv = command_argv(command) if command is not None else []
            consumer = input_consumer(argv)
            if consumer is not None:
                yield ProgramSource(
                    " | ".join(item.text.decode() for item in stages[:index]),
                    consumer,
                    any(shell_generated(item) for item in stages[:index]),
                    True,
                )
        return
    if node.type not in {"heredoc_redirect", "herestring_redirect", "file_redirect"} or node.parent is None:
        return
    pipeline = next((child for child in node.named_children if child.type == "pipeline"), None)
    for command in [node.parent, *(pipeline.named_children if pipeline is not None else [])]:
        command = statement_command(command)
        consumer = input_consumer(command_argv(command) if command is not None else [])
        if consumer is not None:
            yield from redirect_programs(node, consumer)


def redirect_programs(node: Node, consumer: str) -> Iterator[ProgramSource]:
    if node.type == "heredoc_redirect":
        body = next((child for child in node.named_children if child.type == "heredoc_body"), None)
        if body is not None:
            yield node_program(body, consumer)
    elif node.type == "herestring_redirect":
        if node.named_children:
            yield node_program(node.named_children[-1], consumer)
    elif any(child.type == "<" for child in node.children):
        destination = node.child_by_field_name("destination")
        if destination is not None:
            program = node_program(destination, consumer, produced=True)
            program.generated = program.generated or is_interpreter(consumer)
            yield program


SIMPLE_SHELL_NODES = frozenset(
    {
        "command",
        "command_name",
        "command_substitution",
        "comment",
        "compound_statement",
        "concatenation",
        "do_group",
        "expansion",
        "file_descriptor",
        "file_redirect",
        "for_statement",
        "heredoc_body",
        "heredoc_end",
        "heredoc_redirect",
        "heredoc_start",
        "herestring_redirect",
        "list",
        "number",
        "pipeline",
        "program",
        "raw_string",
        "redirected_statement",
        "simple_expansion",
        "special_variable_name",
        "string",
        "string_content",
        "variable_assignment",
        "variable_name",
        "word",
    }
)
SHELL_BUILTINS = frozenset(
    {
        "-",
        ".",
        ":",
        "[",
        "[[",
        "alias",
        "autoload",
        "bg",
        "bind",
        "bindkey",
        "break",
        "builtin",
        "bye",
        "caller",
        "cap",
        "cd",
        "chdir",
        "clone",
        "command",
        "comparguments",
        "compcall",
        "compctl",
        "compdescribe",
        "compfiles",
        "compgen",
        "compgroups",
        "complete",
        "compopt",
        "compquote",
        "comptags",
        "comptry",
        "compvalues",
        "continue",
        "coproc",
        "declare",
        "dirs",
        "disable",
        "disown",
        "echo",
        "emulate",
        "enable",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "fc",
        "fg",
        "float",
        "function",
        "functions",
        "getcap",
        "getln",
        "getopts",
        "hash",
        "hashinfo",
        "help",
        "history",
        "integer",
        "jobs",
        "kill",
        "let",
        "limit",
        "local",
        "log",
        "logout",
        "mapfile",
        "nocorrect",
        "noglob",
        "popd",
        "print",
        "printf",
        "private",
        "pushd",
        "pushln",
        "pwd",
        "r",
        "read",
        "readarray",
        "readonly",
        "rehash",
        "repeat",
        "return",
        "sched",
        "select",
        "set",
        "setcap",
        "setopt",
        "shift",
        "shopt",
        "source",
        "stat",
        "strftime",
        "suspend",
        "syserror",
        "sysopen",
        "sysread",
        "sysseek",
        "syswrite",
        "tcp_open",
        "test",
        "time",
        "times",
        "trap",
        "true",
        "ttyctl",
        "type",
        "typeset",
        "ulimit",
        "umask",
        "unalias",
        "unfunction",
        "unhash",
        "unlimit",
        "unset",
        "unsetopt",
        "vared",
        "wait",
        "whence",
        "where",
        "which",
        "zcompile",
        "zcurses",
        "zformat",
        "zftp",
        "zle",
        "zmodload",
        "zparseopts",
        "zprof",
        "zpty",
        "zregexparse",
        "zselect",
        "zsocket",
        "zstat",
        "zstyle",
        "zsystem",
        "ztcp",
    }
)
VARIABLE_PRESERVING_BUILTINS = frozenset({":", "cd", "echo", "exit", "false", "pwd", "true"})
SHELL_MANAGED_NAMES = frozenset(
    {
        "ARGC",
        "CDPATH",
        "COLUMNS",
        "COPROC",
        "DIRSTACK",
        "EPOCHREALTIME",
        "EPOCHSECONDS",
        "ERRNO",
        "EUID",
        "FPATH",
        "FUNCNAME",
        "GROUPS",
        "HISTCMD",
        "HOSTNAME",
        "LINENO",
        "LINES",
        "MAILPATH",
        "MANPATH",
        "MAPFILE",
        "MATCH",
        "MBEGIN",
        "MEND",
        "MODULE_PATH",
        "OLDPWD",
        "OPTARG",
        "OPTIND",
        "PATH",
        "PIPESTATUS",
        "PPID",
        "PSVAR",
        "PWD",
        "RANDOM",
        "REPLY",
        "SECONDS",
        "SHELLOPTS",
        "SHLVL",
        "SRANDOM",
        "TTYIDLE",
        "UID",
        "_",
        "argv",
        "cdpath",
        "epochtime",
        "fpath",
        "mailpath",
        "manpath",
        "match",
        "mbegin",
        "mend",
        "module_path",
        "path",
        "pipestatus",
        "psvar",
        "reply",
        "status",
    }
)
PLAIN_EXPANSION = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})")
LITERAL_SHELL_VALUE = re.compile(r"[A-Za-z0-9_./@:+,%-][A-Za-z0-9_./@:+,%#-]*")
LITERAL_EXPANSION_PASSES = 64
LITERAL_EXPANSION_BYTES = 1 << 20
LITERAL_LOOP_VALUES = 64


def parse_shell(source: bytes) -> Node:
    return Parser(Language(tree_sitter_bash.language())).parse(source).root_node


def literal_shell_value(node: Node | None) -> str | None:
    if node is None or shell_generated(node):
        return None
    value = shell_word(node)
    return value if LITERAL_SHELL_VALUE.fullmatch(value) else None


def shell_code_nodes(node: Node) -> Iterator[Node]:
    yield node
    if node.type == "heredoc_body" and quoted_heredoc(node):
        return
    for child in node.named_children:
        yield from shell_code_nodes(child)


def quoted_heredoc(body: Node) -> bool:
    start = next((child for child in body.parent.named_children if child.type == "heredoc_start"), None) if body.parent else None
    return start is not None and any(character in start.text for character in b"'\"\\")


def variables_keep_assigned_values(root: Node, *, names_resolved: bool = True) -> bool:
    return all(simple_shell_node(node, names_resolved=names_resolved) for node in shell_code_nodes(root))


def simple_shell_node(node: Node, *, names_resolved: bool = True) -> bool:
    if node.type not in SIMPLE_SHELL_NODES:
        return False
    if node.type == "expansion":
        return PLAIN_EXPANSION.fullmatch(node.text.decode()) is not None
    if node.type == "compound_statement":
        return node.children[0].type == "{"
    if node.type == "for_statement":
        return node.children[0].type == "for" and node.child_by_field_name("variable").text != b"IFS"
    if node.type == "variable_assignment":
        return node.children[1].type == "=" and node.children[0].text != b"IFS"
    if node.type != "command":
        return True
    name = node.child_by_field_name("name")
    if name is None:
        return False
    if shell_generated(name):
        return not names_resolved
    return shell_word(name) in VARIABLE_PRESERVING_BUILTINS or shell_word(name) not in SHELL_BUILTINS


def plain_expansion(node: Node, name: str) -> bool:
    return node.type in {"simple_expansion", "expansion"} and node.text.decode() in {f"${name}", f"${{{name}}}"}


def shell_assignments(root: Node) -> dict[str, list[Node]]:
    assignments: dict[str, list[Node]] = {}
    for node in shell_code_nodes(root):
        if node.type in {"variable_assignment", "for_statement"}:
            name = node.child_by_field_name("name" if node.type == "variable_assignment" else "variable")
            if name is not None:
                assignments.setdefault(name.text.decode(), []).append(node)
    return assignments


def assigned_once(name: str, assignments: dict[str, list[Node]]) -> bool:
    return (
        name not in SHELL_MANAGED_NAMES
        and not name.startswith(("BASH", "COMP_", "READLINE_", "TRY_BLOCK_", "ZSH_"))
        and len(assignments.get(name, [])) == 1
    )


def runs_unconditionally(node: Node) -> bool:
    while node.parent is not None and node.parent.type == "list":
        if node.parent.named_children[0] != node:
            return False
        node = node.parent
    following = node.next_sibling
    return node.parent is not None and node.parent.type == "program" and (following is None or following.type != "&")


def substitutable_uses(root: Node, source: bytes, name: str, start: int, end: int) -> list[Node]:
    return [
        node
        for node in shell_nodes(root)
        if plain_expansion(node, name)
        and start <= node.start_byte
        and node.end_byte <= end
        and source[node.end_byte : node.end_byte + 1] not in {b":", b"["}
        and not any(ancestor.type == "heredoc_body" for ancestor in shell_ancestors(node))
    ]


def shell_ancestors(node: Node) -> Iterator[Node]:
    while node.parent is not None:
        node = node.parent
        yield node


def replaced(source: bytes, edits: list[tuple[int, int, bytes]], offset: int = 0) -> bytes:
    for start, end, value in sorted(edits, reverse=True):
        source = source[: start - offset] + value + source[end - offset :]
    return source


def substitution_edits(uses: list[tuple[Node, str]]) -> list[tuple[int, int, bytes]]:
    substituted = {use.start_byte for use, _ in uses}
    edits = []
    for use, value in uses:
        edits.append((use.start_byte, use.end_byte, value.encode()))
        previous = use.prev_sibling
        if (
            previous is not None
            and previous.type == "simple_expansion"
            and previous.end_byte == use.start_byte
            and previous.start_byte not in substituted
            and re.match(r"\w", value)
        ):
            edits.append((previous.start_byte, previous.end_byte, b"${" + previous.text[1:] + b"}"))
    return edits


def bound_variable_edits(root: Node, source: bytes, assignments: dict[str, list[Node]]) -> list[tuple[int, int, bytes]]:
    uses = []
    for name, nodes in assignments.items():
        assignment = nodes[0]
        if not assigned_once(name, assignments) or assignment.type != "variable_assignment":
            continue
        value = literal_shell_value(assignment.child_by_field_name("value"))
        if value is None or not runs_unconditionally(assignment):
            continue
        uses.extend((use, value) for use in substitutable_uses(root, source, name, assignment.end_byte, len(source)))
    return substitution_edits(uses)


@dataclass
class LiteralLoop:
    node: Node
    name: str
    body: Node
    values: list[str]


def literal_loop(loop: Node, assignments: dict[str, list[Node]]) -> LiteralLoop | None:
    if loop.type != "for_statement":
        return None
    variable = loop.child_by_field_name("variable")
    body = loop.child_by_field_name("body")
    if variable is None or body is None or body.type != "do_group":
        return None
    name = variable.text.decode()
    if not assigned_once(name, assignments):
        return None
    words = loop.children_by_field_name("value")
    values = [value for word in words if (value := literal_shell_value(word)) is not None]
    return LiteralLoop(loop, name, body, values) if 0 < len(values) == len(words) <= LITERAL_LOOP_VALUES else None


def unrolled_loop(root: Node, source: bytes, loop: LiteralLoop) -> bytes:
    start, end = loop.body.children[0].end_byte, loop.body.children[-1].start_byte
    uses = substitutable_uses(root, source, loop.name, start, end)
    copies = [replaced(source[start:end], substitution_edits([(use, value) for use in uses]), start) for value in loop.values]
    return source[: loop.node.start_byte] + b"{\n" + b"\n".join(copies) + b"\n}" + source[loop.node.end_byte :]


def expanded_once(root: Node, source: bytes) -> bytes | None:
    if not variables_keep_assigned_values(root, names_resolved=False):
        return None
    assignments = shell_assignments(root)
    edits = bound_variable_edits(root, source, assignments)
    if edits:
        return replaced(source, edits)
    loops = [loop for node in shell_nodes(root) if (loop := literal_loop(node, assignments)) is not None]
    return unrolled_loop(root, source, loops[-1]) if loops else None


def with_literal_values(root: Node, source: bytes) -> Node:
    expanded = root
    for _ in range(LITERAL_EXPANSION_PASSES):
        following = expanded_once(expanded, source)
        if following is None:
            return expanded if variables_keep_assigned_values(expanded) else root
        expanded = parse_shell(following)
        source = following
        if len(source) > LITERAL_EXPANSION_BYTES or expanded.has_error:
            return root
    return root


def shell_claims(text: str, *, claim_related: bool = False) -> ShellClaims:
    discovery = ShellClaims()
    related = claim_related or ("gh-plan" in text and CLAIM_WORD.search(text) is not None)
    root = parse_shell(text.encode())
    if root.has_error:
        discovery.receipt_pure = False
        discovery.decode_failed = related
        return discovery
    root = with_literal_values(root, text.encode())
    for node in shell_nodes(root):
        if node.type in {
            "pipeline",
            "command_substitution",
            "process_substitution",
            "for_statement",
            "c_style_for_statement",
            "while_statement",
            "function_definition",
            "if_statement",
            "case_statement",
        }:
            discovery.receipt_pure = False
        if node.type == "variable_assignment":
            discovery.opaque = discovery.opaque or (related and environment_executes(node))
        programs = list(input_programs(node))
        if node.type == "command":
            argv = command_argv(node)
            command_sources = list(command_programs(node))
            arguments = claim_arguments(argv)
            if arguments is not None and "claim" in arguments and "--help" not in arguments and "-h" not in arguments:
                discovery.arguments.append(arguments)
            else:
                discovery.receipt_pure = False
                covered = {index for source in command_sources for index in source.argument_indexes}
                generated = related and any(
                    index not in covered and shell_generated(word) for index, word in enumerate(command_words(node))
                )
                discovery.opaque = discovery.opaque or (
                    arguments is None and has_opaque_claim_execution(argv, covered, generated=generated, claim_related=related)
                )
            name = node.child_by_field_name("name")
            discovery.opaque = discovery.opaque or bool(name is not None and shell_generated(name) and related)
            discovery.opaque = discovery.opaque or (
                loads_test_code(argv) and (claim_related or claim_text_outside(root, node))
            )
            programs.extend(command_sources)
        for program in programs:
            discovery.receipt_pure = False
            if program.generated:
                discovery.opaque = discovery.opaque or related
            elif program.produced or not program.consumer:
                discovery.opaque = discovery.opaque or has_claim_text(program.text)
            elif Path(program.consumer).name in SHELL_NAMES | {"eval"}:
                nested = shell_claims(program.text, claim_related=related)
                discovery.arguments.extend(nested.arguments)
                discovery.opaque = discovery.opaque or nested.opaque
                discovery.decode_failed = discovery.decode_failed or nested.decode_failed
            else:
                discovery.opaque = discovery.opaque or has_executable_claim_source(
                    program.text, program.consumer, claim_related=related
                )
    return discovery


def claim_arguments(argv: list[str]) -> list[str] | None:
    position = command_position(argv)
    if position is None or position.index >= len(argv):
        return None
    argv = argv[position.index :]
    executable = Path(argv[0]).name
    if re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", executable):
        argv = argv[1:]
    elif executable != "gh-plan.py":
        return None
    index = next((index for index, argument in enumerate(argv) if Path(argument).name == "gh-plan.py"), None)
    if index is None or any(argument.endswith(".py") for argument in argv[:index]):
        return None
    argv = argv[index:]
    if not argv or Path(argv[0]).name != "gh-plan.py":
        return None
    return argv[1:]


def has_opaque_claim_execution(argv: list[str], covered: set[int], *, generated: bool = False, claim_related: bool = False) -> bool:
    if not argv or accepts_claim_data(argv):
        return False
    if claim_related and data_command_executes(argv):
        return True
    helper = next((index for index, argument in enumerate(argv) if Path(argument).name == "gh-plan.py"), None)
    if helper is not None and "claim" in argv[helper + 1 :]:
        return True
    if "claim" in argv and any("$" in argument or "`" in argument for argument in argv[: argv.index("claim")]):
        return True
    position = command_position(argv)
    offset = position.index + 1 if position is not None else 0
    interpreter = is_interpreter(script_consumer(argv))
    if claim_related and any(
        index not in covered
        and is_program_indicator(argument, argv[index + 1] if index + 1 < len(argv) else "", interpreter=interpreter)
        for index, argument in enumerate(argv)
        if index >= offset
    ):
        return True
    return generated or any(index not in covered and has_claim_text(argument) for index, argument in enumerate(argv))


def is_program_indicator(argument: str, following: str = "", *, interpreter: bool = False) -> bool:
    if is_interpreter(argument) or argument in {"eval", "source"} or re.match(r"--(?:eval|print)(?:=|$)", argument):
        return True
    if argument == ".":
        return following in {"-", "/dev/stdin", "/dev/fd/0"}
    if interpreter:
        return re.match(r"-[A-Za-z]*[ceE]", argument) is not None
    flag = re.fullmatch(r"-[ceE](.*)", argument, re.S)
    return flag is not None and any(marker in (flag[1] or following) for marker in "$`();{}\n")


def is_evaluator_flag(argument: str, consumer: str) -> bool:
    name = Path(consumer).name
    if name in SHELL_NAMES or re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", name):
        letters = "c"
    elif name in {"node", "nodejs", "deno", "bun", "perl", "ruby"}:
        letters = "eE" if name == "perl" else "ep" if name in {"node", "nodejs", "bun"} else "e"
    else:
        return False
    return (
        argument == "--eval"
        or (argument == "--print" and "p" in letters)
        or re.fullmatch(rf"-[A-Za-z]*[{letters}]", argument) is not None
    )


def invocation_target(arguments: list[str], session: str) -> dict | None:
    options = {}
    positionals = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument.startswith("-"):
            key, equal, value = argument.partition("=")
            if not equal:
                index += 1
                value = arguments[index] if index < len(arguments) else ""
            options[key] = value
        else:
            positionals.append(argument)
        index += 1
    if not positionals or positionals[0] != "claim":
        return None
    declared = options.get("--session", "")
    native = declared if declared and not any(character in declared for character in "$`\\") else None
    repo = options.get("--repo", options.get("-R", ""))
    target = positionals[1] if len(positionals) > 1 else ""
    qualified = re.fullmatch(r"(?:https://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:#|/(?:issues|pull)/)([1-9]\d*)", target)
    known = (
        (qualified[1], int(qualified[2]))
        if qualified
        else (
            (repo, int(target)) if claim_log.REPOSITORY_PATTERN.fullmatch(repo) and target.isdecimal() and int(target) > 0 else None
        )
    )
    return {
        "known": known,
        "number": int(target) if target.isdecimal() else None,
        "session": native,
        "foreign": native is not None and native != session,
    }


def resolve_invocations(
    arguments_list: list[list[str]], blocks: list[object], session: str, *, receipt_pure: bool = True
) -> tuple[set[tuple[str, int]], bool]:
    results = list({json.dumps(item, sort_keys=True): item for block in blocks for item in values(block)}.values())
    completed = [
        (item["claim"]["session"], target)
        for item in results
        if isinstance(item.get("claim"), dict)
        and isinstance(item["claim"].get("session"), str)
        and (target := claim_target(item, item["claim"]["session"])) is not None
    ]
    targets = {target for native, target in completed if native == session}
    if any(item.get("foreign_native_output") for item in results):
        return targets, False
    rejected = [
        item
        for item in results
        if item.get("operation") == "github.plan.claim"
        and item.get("ok") is False
        and item.get("write_outcome") in {"not_started", "rejected"}
        and not item.get("claim_comment")
        and "post_claim" not in item.get("completed_steps", [])
    ]
    invocations = [item for arguments in arguments_list if (item := invocation_target(arguments, session)) is not None]
    invocations.sort(key=lambda item: not (item["known"] or item["foreign"]))
    missing = 0
    unproven = False
    for item in invocations:
        if not receipt_pure and not item["known"] and not item["foreign"]:
            unproven = True
            continue
        match = next(
            (
                index
                for index, (native, receipt) in enumerate(completed)
                if (item["session"] is None or native == item["session"])
                and (receipt == item["known"] or (item["known"] is None and receipt[1] == item["number"]))
            ),
            None,
        )
        if match is not None:
            completed.pop(match)
        elif item["known"] or item["foreign"]:
            if rejected:
                rejected.pop()
            if item["known"] and not item["foreign"]:
                targets.add(item["known"])
        else:
            missing += 1
    return targets, unproven or missing > len(rejected)


def orphan_artifacts(records: list[dict], session: str, directory: Path) -> list[str]:
    inputs = {
        str(block.get("id", ""))
        for row in records
        for content in [(row.get("message") or {}).get("content")]
        if isinstance(content, list)
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }
    missing = set()
    for row in records:
        result = row.get("toolUseResult") or {}
        content = (row.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result" or str(block.get("tool_use_id", "")) in inputs:
                continue
            path = result.get("persistedOutputPath") if isinstance(result, dict) else None
            if (
                isinstance(path, str)
                and Path(path).parent.name == "tool-results"
                and Path(path).parent.parent.name not in {session, directory.parent.name}
            ):
                continue
            preview = block.get("content")
            if (
                path
                or (isinstance(result, dict) and result.get("backgroundTaskId"))
                or (isinstance(preview, str) and preview.startswith("<persisted-output>"))
            ):
                missing.add(str(block.get("tool_use_id", "")))
    return sorted(missing)


def literal_targets(records: list[dict], session: str, *, directory: Path | None = None) -> dict:
    targets = set()
    errors = []
    outputs = tool_outputs(records)
    denied = denied_invocations(records)
    if directory is not None:
        errors.extend(
            {
                "call_id": identifier,
                "reason": "missing_invocation",
                "recovery": "Restore this artifact's matching native tool invocation on the claiming host; its command is needed to establish claim coverage.",
            }
            for identifier in orphan_artifacts(records, session, directory)
        )
    claim_may_be_stored = False
    claim_may_be_interactive = False
    for identifier, text, javascript in tool_inputs(records):
        if identifier in denied:
            continue
        names = javascript_names(text) if javascript else set()
        executable_text = without_patch_text(text) if javascript else text
        claim_related_here = "gh-plan" in executable_text or CLAIM_WORD.search(executable_text) is not None
        invocations = []
        opaque = False
        unclassified = False
        reason = "missing_output"
        decode_failed = False
        receipt_pure = not javascript
        try:
            sources = shell_texts(text, javascript)
            opaque = sources.opaque
            claim_may_be_interactive = (
                claim_may_be_interactive
                or (
                    claim_related_here
                    and (
                        sources.interactive_opaque
                        or any(
                            type(value.get("session_id")) is int for block in outputs.get(identifier, []) for value in values(block)
                        )
                    )
                )
                or any(
                    source.interactive and ("gh-plan" in source.text or CLAIM_WORD.search(source.text))
                    for source in sources.commands
                )
            )
            unclassified = opaque
            opaque = opaque and (
                claim_related_here
                or (claim_may_be_stored and "load" in names)
                or (sources.interactive_opaque and claim_may_be_interactive)
            )
            for source in sources.commands:
                receipt_pure = receipt_pure and not source.interactive
                discovery = shell_claims(source.text, claim_related=source.interactive and claim_may_be_interactive)
                invocations.extend(discovery.arguments)
                opaque = opaque or discovery.opaque
                receipt_pure = receipt_pure and discovery.receipt_pure
                if discovery.decode_failed:
                    decode_failed = True
                    reason = "decode_failure: native claim shell syntax cannot be decoded"
        except ValueError as error:
            decode_failed = True
            reason = "decode_failure: " + str(error)
        opaque = opaque or (
            not receipt_pure
            and any(
                item is not None and item["known"] is None and not item["foreign"]
                for arguments in invocations
                for item in [invocation_target(arguments, session)]
            )
        )
        blocks = outputs.get(identifier, [])
        found, unknown = resolve_invocations(invocations, blocks, session, receipt_pure=receipt_pure)
        targets.update(found)
        if javascript:
            opaque = opaque or (unclassified and bool(found))
            claim_may_be_stored = claim_may_be_stored or ("store" in names and (claim_related_here or bool(found)))
        if opaque:
            reason = "unproven_execution_count" if invocations and not receipt_pure else "computed"
        if (unknown or opaque) and directory is not None:
            try:
                saved = persisted_output(records, directory, session, identifier)
                blocks = [*blocks, *tool_outputs(saved).get(identifier, [])]
                found, unknown = resolve_invocations(invocations, blocks, session, receipt_pure=receipt_pure)
                targets.update(found)
            except (OSError, ValueError) as error:
                reason = "missing_output: " + str(error)
        if unknown or opaque or decode_failed:
            errors.append(
                {
                    "call_id": identifier,
                    "reason": reason,
                    "recovery": "Restore this invocation's complete native command and output on the claiming host; keep unknown writes conditional. If evidence is irrecoverable, record the Director's decision on the owning issue.",
                }
            )
    return {"targets": targets, "unresolved_invocations": errors}
