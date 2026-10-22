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
    for blocks in list(outputs.values()):
        yielded = {item["session_id"] for block in blocks for item in values(block) if type(item.get("session_id")) is int}
        if not yielded:
            continue
        for followup, text in inputs.items():
            if any(re.search(rf"session_id[\"']?\s*:\s*{process}(?!\d)", text) for process in yielded):
                blocks.extend(outputs.get(followup, []))
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
    unknown_bindings = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(item.asname or item.name for item in node.names if item.name in modules)
        if isinstance(node, ast.ImportFrom) and node.module in modules:
            launchers.update({item.asname or item.name: item.name for item in node.names if pattern.fullmatch(item.name)})
        if isinstance(node, ast.ImportFrom) and any(item.name == "*" for item in node.names):
            unknown_bindings = True
        if isinstance(node, ast.Call) and python_dynamic_import(node.func):
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
                and (unknown_bindings or python_claim_arguments(node))
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
        try:
            value = ast.literal_eval(command)
        except (ValueError, TypeError):
            return True
        if "gh-plan" in str(value) and (CLAIM_WORD.search(str(value)) or name in {"run_path", "run_module"}):
            return True
    return False


def python_dynamic_import(function: ast.AST) -> bool:
    return (isinstance(function, ast.Name) and function.id == "__import__") or (
        isinstance(function, ast.Attribute) and function.attr == "import_module"
    )


def python_data_call(function: ast.AST) -> bool:
    if isinstance(function, ast.Name):
        return function.id in {"print", "repr", "str", "bytes", "dict", "list", "tuple", "set", "frozenset", "Path", "len"}
    return isinstance(function, ast.Attribute) and function.attr in {
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
        "literal_eval",
    }


def python_claim_arguments(call: ast.Call) -> bool:
    arguments = [*call.args, *(item.value for item in call.keywords)]
    return has_claim_text(
        " ".join(
            node.value
            for argument in arguments
            for node in ast.walk(argument)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
    )


def python_launcher_name(node: ast.AST, modules: set[str], launchers: dict[str, str], pattern: re.Pattern) -> str | None:
    if isinstance(node, ast.Name):
        return launchers.get(node.id) if isinstance(node.ctx, ast.Load) else None
    if isinstance(node, ast.Attribute) and pattern.fullmatch(node.attr):
        root = node.value
        while isinstance(root, ast.Attribute):
            root = root.value
        return node.attr if isinstance(root, ast.Name) and root.id in modules else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
        receiver, member = node.args[:2]
        if isinstance(receiver, ast.Name) and receiver.id in modules:
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
    value_flags = {"--python", "-p", "--with", "--with-requirements"}
    boolean_flags = {"--no-project", "--no-config", "--offline", "--no-cache", "--frozen", "--locked", "--no-sync", "--isolated"}
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


def accepts_claim_data(argv: list[str]) -> bool:
    position = command_position(argv)
    if position is None:
        return False
    name = Path(argv[position.index]).name if position.index < len(argv) else ""
    if name in {
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
        "pytest",
        "gh-issue",
        "gh-comment",
    }:
        return True
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
    script = next((argument for argument in arguments if not argument.startswith("-")), "")
    return bool(script) and not any(marker in script for marker in ("$", "`", "<(", ">("))


def input_consumer(argv: list[str]) -> str | None:
    if accepts_claim_data(argv):
        return None
    consumer = script_consumer(argv)
    if reads_program_stdin(argv, consumer):
        return consumer
    if consumer:
        return None
    return ""


def statement_command(node: Node) -> Node | None:
    if node.type == "command":
        return node
    body = node.child_by_field_name("body")
    return statement_command(body) if body is not None else None


def node_program(node: Node, consumer: str, *, produced: bool = False) -> ProgramSource:
    text = node.text.decode() if produced or node.type == "heredoc_body" else shell_word(node)
    return ProgramSource(text, consumer, shell_generated(node), produced)


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
            program = node_program(words[index + 1], consumer)
            program.argument_indexes = (index + 1,)
            yield program
            return
    script = next((word for word, value in zip(words[offset:], argv[offset:], strict=True) if not value.startswith("-")), None)
    if script is not None and (consumer in {"source", "."} or is_interpreter(consumer)):
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
    command = statement_command(node.parent)
    pipeline = next((child for child in node.named_children if child.type == "pipeline"), None)
    if pipeline is not None:
        command = statement_command(pipeline.named_children[-1])
    argv = command_argv(command) if command is not None else []
    consumer = input_consumer(argv)
    if consumer is None:
        return
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
            yield node_program(destination, consumer, produced=True)


def shell_claims(text: str, *, claim_related: bool = False) -> ShellClaims:
    discovery = ShellClaims()
    related = claim_related or ("gh-plan" in text and CLAIM_WORD.search(text) is not None)
    root = Parser(Language(tree_sitter_bash.language())).parse(text.encode()).root_node
    if root.has_error:
        discovery.receipt_pure = False
        discovery.decode_failed = related
        return discovery
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
                    arguments is None and has_opaque_claim_execution(argv, covered, generated=generated)
                )
            name = node.child_by_field_name("name")
            discovery.opaque = discovery.opaque or bool(name is not None and shell_generated(name) and related)
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


def has_opaque_claim_execution(argv: list[str], covered: set[int], *, generated: bool = False) -> bool:
    if not argv or accepts_claim_data(argv):
        return False
    helper = next((index for index, argument in enumerate(argv) if Path(argument).name == "gh-plan.py"), None)
    if helper is not None and "claim" in argv[helper + 1 :]:
        return True
    if "claim" in argv and any("$" in argument or "`" in argument for argument in argv[: argv.index("claim")]):
        return True
    return generated or any(index not in covered and has_claim_text(argument) for index, argument in enumerate(argv))


def is_evaluator_flag(argument: str, consumer: str) -> bool:
    name = Path(consumer).name
    if name in SHELL_NAMES or re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", name):
        letters = "c"
    elif name in {"node", "nodejs", "deno", "bun", "perl", "ruby"}:
        letters = "eE" if name == "perl" else "e"
    else:
        return False
    return argument == "--eval" or re.fullmatch(rf"-[A-Za-z]*[{letters}][A-Za-z]*", argument) is not None


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
        claim_related_here = "gh-plan" in text or CLAIM_WORD.search(text) is not None
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
