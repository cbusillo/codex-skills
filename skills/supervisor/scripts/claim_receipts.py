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
import copy
import argparse
import io
import textwrap
import string
import tokenize
import shutil
import subprocess
import importlib.util
import sys
import builtins
import pprint
from bisect import bisect_left
from functools import lru_cache
from itertools import product
from contextlib import redirect_stdout
from datetime import datetime
import posixpath
from pathlib import Path

CHECK_OPERATION = "work-closeout.claims"
CLAIM_OPERATION = "github.plan.claim"
_SERIAL_NATIVE_RESULT = object()
HIDDEN_UV_RUN_OPTIONS = {"--python-preference": True, "--native-tls": False}
COMMENT = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/([1-9]\d*)#issuecomment-([1-9]\d*)")
JS_STRING = r'''(?:"(?:[^"\\\r\n]|\\(?:\r\n|[\s\S]))*"|'(?:[^'\\\r\n]|\\(?:\r\n|[\s\S]))*'|(?:String\.raw\s*)?`(?:[^`\\]|\\[\s\S])*`)'''
SHELL_FIELD = re.compile(rf'''\btools\.exec_command\s*\(\s*\{{(?:(?:{JS_STRING})|[^{{}}"\'`])*?(?:\b(?:cmd|command)|"(?:cmd|command)"|\'(?:cmd|command)\')\s*:\s*({JS_STRING})(?=\s*[,}}])''')
OWNERSHIP_TEXT = re.compile(r"gh-plan\.py[\s\S]*\bclaim\b|gh-pr\.py[\s\S]*\bcreate\b|\bgh\s+pr\s+create\b")


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


def outputs(records, harness, *, python_checks=None):
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
                                         opaque_calls=file_ownership_executions(records, harness, python_checks=python_checks)):
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
        for current in (value, *javascript_generated_sources(value)):
            for m in composed_shell_fields(current):
                try:
                    texts.append(composed_string(m[1], current))
                except (ValueError, SyntaxError):
                    pass
    else:
        return
    yield from texts



def javascript_projection(native, *, _facts=None):
    """Share inert-atom discovery without dropping any executable suffix."""
    atoms, code = [], list(native)
    string_token = re.compile(JS_STRING)
    regex = re.compile(r"/(?:\\[^\r\n\u2028\u2029]|\[(?:\\[^\r\n\u2028\u2029]|[^\]\\\r\n\u2028\u2029])*\]|[^/\\[\r\n\u2028\u2029])+/[A-Za-z]*")
    complete, templates = True, {}
    parentheses, control_ends = [], set()

    def previous(cursor):
        # Only the preceding token and punctuation matter here. Reading the
        # whole projected prefix at every slash was quadratic in cell length.
        end = cursor
        while end and code[end - 1].isspace():
            end -= 1
        begin = end
        while begin and (code[begin - 1].isalnum() or code[begin - 1] in "_$"):
            begin -= 1
        before = begin
        while before and code[before - 1].isspace():
            before -= 1
        return end, "".join(code[begin:end]), code[before - 1] if before else ""

    def mask(start, end):
        atom = re.compile(r"[\s\S]{" + str(end - start) + "}").match(native, start)
        atoms.append(atom)
        code[start:end] = ["\n" if c in "\r\n\u2028\u2029" else " " for c in atom[0]]

    def template(start):
        nonlocal complete
        cursor, part, operands = start + 1, start, []
        while cursor < len(native):
            if native[cursor] == "\\":
                cursor += 2
            elif native[cursor] == "`":
                mask(part, cursor + 1)
                templates[start] = cursor + 1, tuple(operands)
                return cursor + 1
            elif native.startswith("${", cursor):
                mask(part, cursor + 2)
                begin = cursor + 2
                cursor = scan(cursor + 2, interpolation=True)
                operands.append((begin, cursor - 1))
                part = cursor - 1 if cursor and native[cursor - 1] == "}" else cursor
            else:
                cursor += 1
        complete = False
        return len(native)

    def scan(cursor, interpolation=False):
        nonlocal complete
        braces = 0
        while cursor < len(native):
            start, char = cursor, native[cursor]
            if native.startswith("//", cursor):
                match = re.match(r"//[^\r\n\u2028\u2029]*", native[cursor:])
                cursor += match.end()
                mask(start, cursor)
            elif native.startswith("/*", cursor):
                end = native.find("*/", cursor + 2)
                if end < 0:
                    complete = False
                    return len(native)
                cursor = end + 2
                mask(start, cursor)
            elif char in "\"'":
                atom = string_token.match(native, cursor)
                if atom is None:
                    complete = False
                    return len(native)
                cursor = atom.end()
                mask(start, cursor)
            elif char == "`":
                cursor = template(cursor)
            elif char == "/":
                end, word, before = previous(cursor)
                keyword_prefix = word in {"return", "throw", "case", "delete", "typeof", "void", "yield", "await", "in", "else", "do"} and before != "."
                expression_start = (not end or code[end - 1] in "=(:,[!&|?{;" or keyword_prefix
                                    or end >= 2 and code[end - 2:end] == ["=", ">"]
                                    or end - 1 in control_ends)
                possible = regex.match(native, cursor)
                atom = possible if expression_start else None
                if (possible is not None and atom is None and not word and end and code[end - 1] not in ")]"
                        and (any(c in possible[0] for c in "\"'`") or "//" in possible[0] or "/*" in possible[0])):
                    # In an ambiguous grammar position, a possible regex must
                    # not let its internal quotes/slashes erase a real suffix.
                    complete = False
                cursor = atom.end() if atom is not None else cursor + 1
                if atom is not None:
                    mask(start, cursor)
            elif char == "(":
                end, word, before = previous(cursor)
                for_await = word == "await" and previous(end - len(word))[1] == "for"
                parentheses.append((word in {"if", "while", "for", "with", "switch", "catch"} or for_await) and before != ".")
                cursor += 1
            elif char == ")":
                if parentheses and parentheses.pop():
                    control_ends.add(cursor)
                cursor += 1
            elif char == "}":
                if interpolation and braces == 0:
                    return cursor + 1
                braces -= 1
                cursor += 1
            else:
                braces += char == "{"
                cursor += 1
        if interpolation:
            complete = False
        return cursor

    try:
        scan(0)
    except RecursionError:
        # Deeply nested templates remain unparsed discovery input rather than
        # escaping the checker or being mistaken for a completed projection.
        complete = False
    code = "".join(code)
    if _facts is not None:
        _facts.update(templates=templates)
    admission_facts = {}
    list(javascript_source_calls(native, code, literal_atoms=atoms, templates=templates, _facts=admission_facts))
    regions = list(admission_facts.get("source_only_regions", ()))
    return atoms, code, regions, complete


def javascript_literal_members(native, code, begin, end, *, _facts=None):
    """Share literal aggregate property names and their original value spans."""
    while begin < end and code[begin].isspace():
        begin += 1
    while end > begin and code[end - 1].isspace():
        end -= 1
    opening = code[begin:begin + 1]
    if opening not in {"{", "["} or code[end - 1:end] != ("}" if opening == "{" else "]"):
        return None
    if node_expression_parts(code, begin + 1, end, "", first=True)[0][1] != end - 1:
        return None
    if not native[begin + 1:end - 1].strip():
        return []
    object_, values = opening == "{", []
    for index, (start, finish) in enumerate(node_expression_parts(code, begin + 1, end - 1)):
        parts = node_expression_parts(code, start, finish, ":") if object_ else []
        key_recipe = None
        if not object_:
            name, operand = (None if code[start:finish].lstrip().startswith("...") else str(index)), (start, finish)
        else:
            name = native[slice(*parts[0])].strip()
            operand = (parts[0][1] + 1, finish) if len(parts) > 1 else (start, finish)
            method = re.match(r"\s*(?:async\s+)?\*?\s*([A-Za-z_$][\w$]*)\s*\(", code[start:finish]) if len(parts) == 1 else None
            if method:
                name = method[1]
            if not re.fullmatch(r"[A-Za-z_$][\w$]*|\d+", name):
                key_span = parts[0]
                key_begin = key_span[0]
                while key_begin < key_span[1] and native[key_begin].isspace():
                    key_begin += 1
                if native[key_begin:key_begin + 1] == "[":
                    key_recipe = key_begin + 1, key_span[1] - 1
                    try:
                        name = javascript_string(native[slice(*key_recipe)].strip())
                    except ValueError:
                        name = None
                else:
                    try:
                        name = javascript_string(name)
                    except ValueError:
                        name = None
        values.append((name, operand))
        if _facts is not None:
            _facts.append((name, operand, key_recipe))
    return values


def _javascript_recipe_graph(native, code, facts, *, global_this=False, literal_atoms=(), templates=None):
    """Resolve finite lexical recipes, captures and effects in one dependency graph.

    An anchor names lexical sites, not an expanded argument environment. Capture,
    caller and operand edges live in a separate table. All value consumers use
    the same queries; publishing an effect wakes only its actual dependants.
    """
    pairs, owners, parents, bound, functions, declarations = (facts[name] for name in
        ("pairs", "owners", "parents", "bound", "functions", "declarations"))
    atoms = {atom.start(): atom.end() for atom in literal_atoms if not atom[0].startswith(("//", "/*"))}
    atoms_end = set(atoms.values())
    templates = {} if templates is None else templates
    empty = frozenset()
    data = (frozenset({("data",)}), True)
    undefined = (frozenset({("undefined",)}), True)
    states, dependencies, users, stack = {}, {}, {}, []
    component_parent, component_members, cyclic = {}, {}, set()
    computations, active_positions = {}, {}
    choices, copy_sources = {}, {}
    tasks, queued = [], set()
    anchors = {None: {(-1, None, None, ())}}
    variable_events, effect_sites, effects, effect_tokens, effect_origins = {}, [], {}, {}, {}
    literal_cache, body_cache, visible = {}, {}, {}
    literal_getters = set()
    direct_accessors, array_lengths, array_removals = {}, {}, {}
    copy_packets, callback_items, callback_sources, callback_admissions = {}, {}, {}, {}
    callback_guards, callback_inputs = {}, {}
    bindings_by_body, body_sites, call_sites, body_effects = {}, {}, {}, {}
    function_owners = {}
    bind_sites, sources, write_sites, qualifications = {}, {}, {}, {}
    source_admissions = {}
    reference_operands, receiver_edges, accessor_fields = {}, {}, {}
    member_operands, raw_results, raw_sources = {}, {}, {}
    field_remainders, field_delegates, replacements = {}, {}, {}
    escapes, affected, read_sites, body_reads, relation_origins = {}, {}, {}, {}, {}
    expression_roles, callable_arrows, implicit_sites, switch_regions = {}, set(), {}, []
    opaque_activations = {}

    def grow(table, key, values, fact):
        values = tuple(values)
        selected = table.setdefault(key, set())
        before = len(selected)
        selected.update(values)
        new_origin = False
        if (table is affected or table is escapes) and stack:
            for value in values:
                origins = relation_origins.setdefault((fact, value), set())
                new_origin |= stack[-1] not in origins
                origins.add(stack[-1])
        if len(selected) != before or new_origin:
            invalidate(fact)
            if table is escapes:
                invalidate(("escapes",))

    def watch_bucket(fact):
        # Notification and causal predecessor selection are distinct: table
        # growth wakes a consumer before it knows which events can reach it.
        if stack:
            users.setdefault(fact, set()).add(stack[-1])
            dependencies.setdefault(stack[-1], set()).add(fact)

    def affected_at(root, position, anchor):
        fact = ("affected", root)
        watch_bucket(fact)
        eligible = set()
        for origin in tuple(affected.get(root, ())):
            at, creation = origin
            before, read = at, position
            if creation is not None and anchor is not None and creation != anchor and creation[2] == anchor[2]:
                before, read = creation[0], anchor[0]
            if not ordering(owners[before], read, before)[0]:
                continue
            eligible.add(origin)
            for producer in tuple(relation_origins.get((fact, origin), ())):
                token(producer)
        return eligible

    def raw_result(value):
        if value[0] != "unknown" or len(value) != 3:
            return False
        watch_bucket(("raw-result", value))
        return raw_results.get(value, False)

    def role(span):
        begin, end = trim(*span)
        key = begin, end
        if key in expression_roles:
            return expression_roles[key]
        operators, cursor = [], begin
        while cursor < end:
            if cursor in atoms:
                # Projection retains executable template interpolations between
                # atoms; only this original inert segment can be skipped.
                cursor = min(atoms[cursor], end)
                continue
            if code[cursor:cursor + 1] in "([{":
                closing = pairs.get(cursor)
                if closing is not None:
                    cursor = closing + 1
                    continue
            operator = next((op for op in ("?.", "??", "||", "&&") if code.startswith(op, cursor)), None)
            if operator:
                if operator != "?.":
                    operators.append((cursor, operator))
                cursor += 2
                continue
            if code[cursor:cursor + 1] in {",", "?", ":"}:
                operators.append((cursor, code[cursor]))
            cursor += 1
        result = None
        depth, commas = 0, []
        for at, op in operators:
            if op == "?":
                depth += 1
            elif op == ":":
                depth -= 1
            elif op == "," and not depth:
                commas.append(at)
        if commas:
            at = commas[-1]
            result = ",", (begin, at), (at + 1, end)
        else:
            conditional = next((at for at, op in operators if op == "?"), None)
            if conditional is not None:
                depth = 0
                for at, op in operators:
                    if at < conditional:
                        continue
                    depth += (op == "?") - (op == ":")
                    if not depth:
                        result = "?:", (begin, conditional), (conditional + 1, at), (at + 1, end)
                        break
            else:
                for family in ({"||", "??"}, {"&&"}):
                    candidates = [(at, op) for at, op in operators if op in family]
                    if candidates:
                        at, op = candidates[-1]
                        result = op, (begin, at), (at + len(op), end)
                        break
        expression_roles[key] = result
        return result

    def predicate(item, nullish=False):
        return query(("predicate", item, nullish), lambda: compute_predicate(item, nullish))[0]

    def compute_predicate(item, nullish):
        if item[0] == "choice" and item[1][0] == "native-result":
            token(("choices", item))
            selected = set()
            for operand in tuple(choices.get(item, ())):
                selected.update(predicate(operand, nullish))
            return frozenset(selected), len(selected) == 1
        if item[0] == "index":
            return frozenset({False if nullish else bool(item[1])}), True
        selected = set()
        if item[0] == "span":
            span, anchor = item[1:]
            begin, end = trim(*span)
            raw = native[begin:end].strip()
            numeric = re.fullmatch(r"[+-]?\d+(?:\.\d+)?", raw)
            if raw in {"true", "false", "null"} or raw == "undefined" and lexical(raw, begin) is None or numeric:
                value = raw in {"null", "undefined"} if nullish else raw == "true" or bool(numeric and float(raw))
                return frozenset({value}), True
            values, exact = value_of(item)
            if re.fullmatch(r"[A-Za-z_$][\w$]*", code[begin:end]):
                key = raw, begin, anchor
                token(("reference-operands", key))
                for source in tuple(reference_operands.get(key, ())):
                    selected.update(predicate(source, nullish))
                if selected:
                    return frozenset(selected), len(selected) == 1
        else:
            values, exact = value_of(item)
        for value in values:
            if value[0] == "undefined":
                selected.add(nullish)
            elif value[0] == "string":
                selected.add(False if nullish else bool(value[1]))
            elif raw_result(value):
                selected.update((False,) if nullish else (False, True))
            elif value[0] not in {"data", "unknown", "binding"}:
                selected.add(not nullish)
            else:
                selected.update((False, True))
        return frozenset(selected or {False, True}), exact and len(selected) == 1

    def selected_operands(span, anchor):
        parts = role(span)
        if parts is None:
            return (span,)
        op, left, *right = parts
        if op == ",":
            return (right[0],)
        possible = predicate(recipe(left, anchor), op == "??")
        if op == "?:":
            return tuple(part for truth, part in zip((True, False), right) if truth in possible)
        wants = op in {"&&", "??"}
        return tuple(([left] if (not wants) in possible else []) + ([right[0]] if wants in possible else []))

    def receiver_value(anchor, seen=frozenset()):
        if anchor is None or anchor in seen:
            return (frozenset({("global",)}), True) if global_this and anchor is None else (frozenset({("unknown", "this")}), False)
        token(("receivers", anchor))
        values, exact = set(), True
        for edge, receiver, arrow in tuple(receiver_edges.get(anchor, ())):
            selected, known = receiver_value(edge[2], seen | {anchor}) if arrow else value_of(receiver)
            values.update(selected)
            exact &= known
        return frozenset(values or {("unknown", "this")}), exact and bool(values)

    def trim(begin, end, *, unwrap=True):
        while True:
            while begin < end and code[begin].isspace():
                if begin in atoms:
                    break
                if native[begin].isspace():
                    begin += 1
                elif native.startswith("/*", begin):
                    closing = native.find("*/", begin + 2, end)
                    if closing < 0:
                        break
                    begin = closing + 2
                elif native.startswith("//", begin):
                    closing = native.find("\n", begin + 2, end)
                    begin = end if closing < 0 else closing + 1
                else:
                    break
            while end > begin and code[end - 1].isspace():
                if end in atoms_end:
                    break
                if native[end - 1].isspace():
                    end -= 1
                elif native[max(begin, end - 2):end] == "*/":
                    opening = native.rfind("/*", begin, end - 2)
                    if opening < 0:
                        break
                    end = opening
                else:
                    break
            if unwrap and code[begin:begin + 1] == "(" and pairs.get(begin) == end - 1:
                begin, end = begin + 1, end - 1
            else:
                return begin, end

    def preceding(at):
        while at and code[at - 1].isspace():
            at -= 1
        return at

    def member_span(begin, end):
        """One reference split; a computed selector remains an original recipe."""
        if code[end - 1:end] == "]" and end - 1 in pairs:
            opening = pairs[end - 1]
            if opening > begin:
                finish = preceding(opening)
                if code[max(begin, finish - 2):finish] == "?.":
                    finish = preceding(finish - 2)
                return (begin, finish), (opening + 1, end - 1), None
        suffix = re.search(r"(?:\?\.|\.)\s*([A-Za-z_$][\w$]*)\s*$", code[begin:end])
        if suffix:
            return (begin, begin + suffix.start()), None, suffix[1]
        return None

    def selected_member(begin, end, anchor, position):
        selected = member_span(begin, end)
        if selected is None:
            return None
        receiver_span, key_span, name = selected
        values, exact = expression(receiver_span, anchor)
        names, known = keys_for(native[slice(*key_span)], anchor, position) if key_span is not None else ({name}, True)
        if not known:
            names.add(None)
        result = set()
        for field in names:
            projected, certain = project(values, exact, field, position, anchor)
            result.update(projected)
            known &= certain
        if not known and not result:
            result.add(("unknown", position, anchor))
        return frozenset(result), exact and known, recipe(receiver_span, anchor)

    def lexical(name, position):
        owner, trail = syntax_owner(position), []
        # Parameter expressions share invocation ownership with the body, but
        # only formal bindings are visible there. Body declarations are not.
        if owner != owners[position] and not any(
                at in facts.get("parameter_positions", ()) for at in bound.get(owner, {}).get(name, ())):
            owner = owners[position]
        while (owner, name) not in visible:
            trail.append((owner, name))
            if name in bound.get(owner, {}):
                break
            if owner == -1:
                owner = None
                break
            owner = parents.get(owner, -1)
        result = visible.get((owner, name), owner)
        for item in trail:
            visible[item] = result
        return result

    def syntax_owner(position):
        original = owner = owners[position]
        for begin, end, selected in facts.get("parameter_scopes", ()):
            if begin <= position < end and original == owners[begin]:
                owner = selected
        return owner

    def function_owner(position):
        if position not in function_owners:
            scope = syntax_owner(position)
            while scope != -1 and scope not in functions:
                scope = parents.get(scope, -1)
            function_owners[position] = scope
        return function_owners[position]

    def dominates(context, position, at):
        if at > position:
            return False
        for begin, end, labels in switch_regions:
            if begin < at < end and begin < position < end and any(at < label <= position for label in labels):
                return False
        scope = owners[position]
        while scope != context:
            if scope == -1 or scope in functions:
                return False
            scope = parents.get(scope, -1)
        return True

    def phase(loop, position):
        for kind, begin, end in loop:
            if begin <= position < end:
                return kind
        return None

    def ordering(context, position, at):
        """Possible predecessor and proved dominance in this activation."""
        if function_owner(write_sites.get(at, at)) != function_owner(position):
            return True, False
        may = at < position
        definite = dominates(context, position, at)
        for loop in facts.get("loops", ()):
            if function_owner(min(begin for _, begin, _ in loop)) != function_owner(position):
                continue
            write, read = phase(loop, at), phase(loop, position)
            if write in {"body", "update"} and read is None:
                # Reaching an outside read does not prove that this loop ran.
                definite = False
            if write is None or read is None:
                continue
            if read == "once":
                if write != "once":
                    return False, False
            elif write != "once":
                may = True
                if write == "update" and read in {"condition", "body"}:
                    definite = False
                if write == "body" and read == "update":
                    body_begin = next(begin for kind, begin, _ in loop if kind == "body")
                    # A first unconditional body write cannot be skipped by a
                    # preceding continue/branch. Anything less proved stays a
                    # possible predecessor, never an exact replacement.
                    prefix = code[body_begin:write_sites.get(at, at)].strip()
                    if prefix.startswith("{"):
                        prefix = prefix[1:].strip()
                    definite = re.fullmatch(r"(?:(?:const|let|var)\s+)?", prefix) is not None
        return may, definite

    def event_order(at, position):
        for loop in reversed(facts.get("loops", ())):
            if function_owner(min(begin for _, begin, _ in loop)) != function_owner(position):
                continue
            if phase(loop, position) == "update":
                selected = phase(loop, at)
                if selected is not None:
                    begin = min(start for _, start, _ in loop)
                    return begin, {"once": 0, "condition": 1, "body": 2, "update": 3}[selected], at
        return at, 0, at

    def schedule(site, anchor, *, target=False):
        key = ("target" if target else "invoke"), site, anchor
        if key not in queued:
            queued.add(key)
            tasks.append(key)

    def component(key):
        parent = component_parent.setdefault(key, key)
        if parent != key:
            component_parent[key] = component(parent)
        return component_parent[key]

    def mark_dirty(key):
        state = states.get(key)
        if state is None:
            return
        if state[2] or state[3]:
            state[4] += 1
        state[2] = False
        task = ("query", key)
        if task not in queued:
            queued.add(task)
            tasks.append(task)

    def invalidate(token, *, producer=None):
        # Growing table facts keep their producing query edge as well as the
        # direct consumers. Cycles through effects/captures are graph cycles.
        if token not in states and (producer is not None or stack):
            effect_tokens.setdefault(token, set()).add(producer if producer is not None else stack[-1])
        if token in states:
            mark_dirty(token)
        for user in tuple(users.get(token, ())):
            mark_dirty(user)

    def join_component(key):
        root = component(key)
        members = stack[active_positions[key]:]
        if root in cyclic and all(member in component_members[root] for member in members):
            return
        roots = {component(member) for member in members}
        root = max(roots, key=lambda item: len(component_members.get(item, {item})))
        selected = component_members.setdefault(root, {root})
        for other in roots - {root}:
            component_parent[other] = root
            selected.update(component_members.pop(other, {other}))
            cyclic.discard(other)
        cyclic.add(root)
        for member in selected:
            if states[member][1]:
                states[member][1] = False
                for user in tuple(users.get(member, ())):
                    mark_dirty(user)

    def query(key, compute):
        computations[key] = compute
        if stack:
            users.setdefault(key, set()).add(stack[-1])
            dependencies.setdefault(stack[-1], set()).add(key)
        state = states.setdefault(key, [empty, False, False, False, 0])
        root = component(key)
        component_members.setdefault(root, {root})
        if state[3]:
            join_component(key)
            return state[0], False
        if state[2]:
            return state[0], state[1]
        for dep in dependencies.pop(key, ()):
            users.get(dep, set()).discard(key)
        state[3] = True
        generation = state[4]
        active_positions[key] = len(stack)
        stack.append(key)
        try:
            values, exact = compute()
        finally:
            stack.pop()
            active_positions.pop(key)
            state[3] = False
        if component(key) in cyclic:
            exact = False
            if isinstance(values, dict):
                joined = dict(state[0]) if isinstance(state[0], dict) else {}
                for field, item in values.items():
                    previous = joined.get(field)
                    if isinstance(previous, frozenset) and isinstance(item, frozenset):
                        item = previous | item
                    elif previous is not None and previous != item:
                        choice = ("choice", key, field)
                        edges = choices.setdefault(choice, set())
                        before = len(edges)
                        edges.update(value for value in (previous, item) if value != choice)
                        if len(edges) != before:
                            invalidate(("choices", choice), producer=key)
                        item = choice
                    joined[field] = item
                values = joined
            else:
                values = state[0] | values
        changed = state[0] != values or state[1] != exact
        state[:3] = values, exact, generation == state[4]
        if changed:
            for user in tuple(users.get(key, ())):
                mark_dirty(user)
        if not state[2]:
            mark_dirty(key)
        return values, exact

    def token(key):
        if stack:
            users.setdefault(key, set()).add(stack[-1])
            dependencies.setdefault(stack[-1], set()).add(key)
            if key in states and states[key][3]:
                join_component(key)
            for producer in tuple(effect_tokens.get(key, ())):
                users.setdefault(producer, set()).add(stack[-1])
                dependencies.setdefault(stack[-1], set()).add(producer)
                if states[producer][3]:
                    join_component(producer)

    def callable_body(span):
        if span in body_cache:
            return body_cache[span]
        begin, end = trim(*span)
        result = None
        match = re.match(r"(?P<async>async\s+)?(?:function\b\s*(?P<generator>\*)?\s*(?:[A-Za-z_$][\w$]*\s*)?|(?:(?:get|set)\s+)?(?P<method_generator>\*)?\s*[A-Za-z_$][\w$]*\s*)\(", code[begin:end])
        if match:
            opening = begin + match.end() - 1
            closing = pairs.get(opening)
            body = closing + 1 if closing is not None else end
            while body < end and code[body].isspace():
                body += 1
            if code[body:body + 1] == "{" and pairs.get(body) == end - 1:
                result = (body, end, True, bool(match["async"]),
                    bool(match["generator"] or match["method_generator"]),
                    tuple(node_expression_parts(code, opening + 1, closing)))
        if result is None:
            asynchronous = re.match(r"async\b\s*", code[begin:end])
            starts = ([begin + asynchronous.end()] if asynchronous else []) + [begin]
            for parameter in starts:
                if code[parameter:parameter + 1] == "(":
                    closing = pairs.get(parameter)
                    following = closing + 1 if closing is not None else end
                else:
                    identifier = re.match(r"[A-Za-z_$][\w$]*", code[parameter:end])
                    following = parameter + identifier.end() if identifier else end
                while following < end and code[following].isspace():
                    following += 1
                if code[following:following + 2] != "=>":
                    continue
                body, finish = trim(following + 2, end, unwrap=False)
                block = code[body:body + 1] == "{"
                if body < end and (not block or pairs.get(body) == finish - 1):
                    parameters = tuple(node_expression_parts(code, parameter + 1, closing)) if code[parameter:parameter + 1] == "(" else ((parameter, following),)
                    result = (body, finish, block, bool(asynchronous and parameter != begin), False, parameters)
                    callable_arrows.add((begin, end))
                    break
        body_cache[span] = result
        return result

    def literal(span):
        if span not in literal_cache:
            recipes = []
            members = javascript_literal_members(native, code, *span, _facts=recipes)
            if code[span[0]:span[0] + 1] == "[" and recipes and trim(*recipes[-1][1])[0] == trim(*recipes[-1][1])[1]:
                recipes.pop()
                if members:
                    members = members[:-1]
            literal_cache[span] = members, tuple(recipes)
        return literal_cache[span][0]

    def roots_global(name):
        if name in {"globalThis", "global", "window", "self"} or name == "this" and global_this:
            return ("global",)
        if name == "import":
            return ("handle", "import")
        if name in {"eval", "Function", "AsyncFunction"}:
            return ("handle", "eval" if name == "eval" else "constructor")
        if name in {"String", "Object", "Reflect"}:
            return ("intrinsic", name)
        if name == "text":
            return ("read-callable",)
        if name == "undefined":
            return ("undefined",)
        return None

    def callee(opening):
        end = preceding(opening)
        if code[max(0, end - 2):end] == "?.":
            end = preceding(end - 2)
        begin = end
        while begin:
            paired = code[begin - 1] in ")]" and begin - 1 in pairs
            if paired:
                begin = pairs[begin - 1]
            else:
                finish = begin
                while begin and (code[begin - 1].isalnum() or code[begin - 1] in "_$"):
                    begin -= 1
                if begin == finish:
                    break
                before = preceding(begin)
                if code[begin:finish] in {"await", "return", "typeof", "void", "new", "delete", "yield", "throw", "case", "in", "of"} and (not before or code[before - 1] != "."):
                    begin = finish
                    break
            before = preceding(begin)
            if before and code[before - 1] == ".":
                begin = before - 1
                if begin and code[begin - 1] == "?":
                    begin -= 1
            elif paired and before == begin and before and (code[before - 1] in ")]_$" or code[before - 1].isalnum()):
                continue
            else:
                break
        return trim(begin, end, unwrap=False)

    # All syntax descriptors exist before the first graph query. Targets use
    # the binding owner's walker, including computed/rest/default recipes.
    starts, start = [], 0
    for position, char in enumerate(code):
        starts.append(start)
        if char in ";{}\n":
            start = position + 1

    def independent_statement(begin, prefix):
        if re.fullmatch(prefix, code[starts[begin]:begin]) is None:
            return False
        at = starts[begin]
        if not at or code[at - 1] != "\n":
            return True
        while at and code[at - 1].isspace() and at not in atoms_end:
            at -= 1
        if not at or at in atoms_end:
            return True
        previous = at - 1
        if code[previous] == ")" and pairs.get(previous) in facts.get("headers", ()):
            return False
        if code[previous] in "?=:,&|!+-*/%^<>~":
            return False
        word = at
        while word and (code[word - 1].isalnum() or code[word - 1] in "_$"):
            word -= 1
        return code[word:at] not in {"else", "do"}

    for match in re.finditer(r"(?<![=!<>])=(?!=|>)", code):
        finish_target = preceding(match.start())
        if not finish_target:
            continue
        begin_target, _ = callee(finish_target)
        if code[finish_target - 1] in "}]" and finish_target - 1 in pairs:
            opening = pairs[finish_target - 1]
            before = preceding(opening)
            if ((opening, finish_target) in facts.get("targets", ())
                    or not before or not re.match(r"[\w$)\]]", code[before - 1])):
                begin_target = opening
        if begin_target >= finish_target:
            continue
        rhs_end = node_expression_parts(code, match.end(), len(code), ",;\n", first=True)[0][1]
        write_sites[rhs_end - 1] = begin_target
        target = begin_target, finish_target
        projected = []
        list(javascript_binding_names(code, *target, _facts=projected, _native=native))
        context = parents.get(begin_target, owners[begin_target]) if code[begin_target:begin_target + 1] == "{" else owners[begin_target]
        certain = (independent_statement(begin_target, r"\s*(?:(?:const|let|var)\s+)?")
                   or (begin_target, finish_target, match.start()) in facts.get("update_assignments", ()))
        for selected, path, defaults, rest in projected:
            if selected[0] in facts.get("parameter_positions", ()):
                continue
            event = (selected, path, defaults, rest, (match.end(), rhs_end), begin_target, context, certain)
            raw = code[slice(*selected)].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", raw):
                owner = lexical(raw, selected[0])
                variable_events.setdefault((owner, raw), []).append(event)
                if owner is None:
                    effect_sites.append(event)
            else:
                effect_sites.append(event)
    for opening, char in enumerate(code):
        if char != "(" or opening not in pairs:
            continue
        begin, finish = callee(opening)
        close = pairs[opening]
        following = close + 1
        while following < len(code) and code[following].isspace():
            following += 1
        if begin == finish or code[following:following + 1] == "{" or opening in facts.get("headers", set()):
            continue
        parts = tuple(node_expression_parts(code, opening + 1, close))
        if parts and trim(*parts[-1])[0] == trim(*parts[-1])[1]:
            parts = parts[:-1]
        call_sites[opening] = (begin, finish, close, False, parts)
    for opening, (end, parts) in templates.items():
        begin, finish = callee(opening)
        if begin != finish:
            call_sites[opening] = begin, finish, end - 1, True, (None, *parts)
    # A direct caller can omit template facts for a noninterpolated literal.
    for match in re.finditer(JS_STRING, native):
        if match[0].startswith("`") and "${" not in match[0] and match.start() not in call_sites:
            begin, finish = callee(match.start())
            if begin != finish:
                call_sites[match.start()] = begin, finish, match.end() - 1, True, (None,)
    for site in call_sites:
        body_sites.setdefault(function_owner(site), []).append(site)
    for event in effect_sites:
        body_effects.setdefault(function_owner(event[5]), []).append(event)
    for match in re.finditer(r"(?<![\w.$])switch\s*\(", code):
        close = pairs.get(match.end() - 1)
        begin = close + 1 if close is not None else len(code)
        while begin < len(code) and code[begin].isspace():
            begin += 1
        end = pairs.get(begin)
        if code[begin:begin + 1] != "{" or end is None:
            continue
        labels, cursor = [], begin + 1
        label_token = re.compile(r"(?<![\w$.])(?:case|default)\b")
        while cursor < end:
            label = label_token.match(code, cursor, end)
            before = preceding(cursor)
            if label and (not before or code[before - 1] != "."):
                labels.append(cursor)
            if code[cursor] in "([{":
                cursor = pairs.get(cursor, cursor) + 1
            else:
                cursor += 1
        switch_regions.append((begin, end, tuple(labels)))

    def recipe(span, anchor):
        return ("span", span, anchor) if span is not None else ("value", ("data",))

    def value_of(item):
        return query(("recipe", item), lambda: compute_recipe(item))

    def compute_recipe(item):
        if item[0] == "index":
            return data
        if item[0] == "array-tail":
            values, exact = set(), True
            for source in tail_operands(*item[1:]):
                selected, certain = value_of(source)
                values.update(selected)
                exact &= certain
            return frozenset(values), exact
        if item[0] == "choice":
            token(("choices", item))
            values = set()
            for selected in choices.get(item, ()):
                values.update(value_of(selected)[0])
            return frozenset(values), False
        if item[0] == "snapshot-member":
            source, field, site, anchor = item[1:]
            values, exact = value_of(source)
            return project(values, exact, field, site, anchor)
        if item[0] == "span":
            return expression(item[1], item[2])
        if item[0] == "path":
            source, path, defaults, rest, position, anchor = item[1:]
            values, exact = value_of(source)
            for depth in range(len(path) + 1):
                for at, default in defaults:
                    if at == depth and (not values or any(value[0] in {"undefined", "unknown", "binding"} for value in values)):
                        fallback, fallback_exact = expression(default, anchor)
                        missing = not values or values <= frozenset({("undefined",)})
                        values = fallback if missing else values | fallback
                        exact &= fallback_exact
                if depth == len(path):
                    break
                field = path[depth]
                if isinstance(field, tuple) and field[0] == "key":
                    fields, certain = keys_for(native[slice(*field[1])], anchor, position)
                else:
                    fields, certain = ({field}, True) if field is not None else (set(), False)
                selected, known = set(), exact and certain
                for field in fields:
                    result, definite = project(values, exact, field, position, anchor)
                    selected.update(result)
                    known &= definite
                values = frozenset(selected) if certain else frozenset(selected | {("unknown",)})
                exact = known
            if rest is not None:
                return frozenset({("rest", item)}), exact
            return values, exact
        if item[0] == "descriptor":
            source, site, anchor = item[1:]
            values, exact = value_of(source)
            selected, getters = set(), set()
            if item in direct_accessors:
                for getter in direct_accessors[item][0] or ():
                    getters.update(value_of(getter)[0])
            elif source in literal_getters:
                getters.update(values)
            else:
                for root in values:
                    fields, known = aggregate_fields(root, site) if root[0] == "aggregate" else ({}, False)
                    exact &= known
                    if "value" in fields:
                        results, certain = value_of(fields["value"])
                        selected.update(results)
                        exact &= certain
                    if "get" in fields:
                        getters.update(value_of(fields["get"])[0])
            if getters:
                results, certain = invoke_values(frozenset(getters), ("value", ("undefined",)), (), site, anchor, set())
                selected.update(results)
                exact &= certain
            return frozenset(selected), exact
        if item[0] == "args":
            return frozenset({item}), True
        return frozenset({item[1]}), item[1][0] != "unknown" or raw_result(item[1])

    def numeric_field(field):
        return isinstance(field, str) and re.fullmatch(r"0|[1-9][0-9]*", field) is not None

    def operand_keys(item, seen=frozenset()):
        if item in seen:
            return set(), False
        if item[0] == "index":
            return {str(item[1])}, True
        if item[0] == "path" and not item[2] and item[4] is None:
            return operand_keys(item[1], seen | {item})
        if item[0] == "span":
            begin, end = trim(*item[1])
            raw = native[begin:end].strip()
            if numeric_field(raw):
                return {raw}, True
        values, exact = value_of(item)
        names = {value[1] for value in values if value[0] == "string"}
        return names, exact and bool(values) and len(names) == len(values)

    def keys_for(raw, anchor, position):
        raw = raw.strip()
        try:
            if re.fullmatch(JS_STRING + r"(?:\s*\+\s*" + JS_STRING + r")*", raw):
                return {"".join(javascript_string(atom[0]) for atom in re.finditer(JS_STRING, raw))}, True
        except ValueError:
            pass
        if numeric_field(raw):
            return {raw}, True
        if re.fullmatch(r"[A-Za-z_$][\w$]*", raw):
            values, known = reference_value(raw, position, anchor)
            result = {root[1] for root in values if root[0] == "string"}
            if len(result) == len(values):
                return result, known
            key = raw, position, anchor
            token(("reference-operands", key))
            originals = tuple(reference_operands.get(key, ()))
            exact = bool(originals)
            for item in originals:
                names, certain = operand_keys(item)
                result.update(names)
                exact &= certain
            return result, exact
        return set(), False

    def select_path(item, path, defaults, rest, position, anchor):
        return ("path", item, path, defaults, rest, position, anchor) if path or defaults or rest is not None else item

    def anchor_context(anchor, owner, seen=frozenset()):
        if anchor in seen:
            return ()
        if anchor is None:
            return (None,)
        token(("anchor", anchor))
        if anchor[2] == owner:
            return (anchor,)
        result = set()
        for _, caller, capture, _ in anchors[anchor]:
            if capture == anchor:
                continue
            if capture is None:
                result.add(None)
            else:
                result.update(anchor_context(capture, owner, seen | {anchor}))
        return tuple(result) or (None,)

    def parameter_operands(name, owner, anchor):
        result = set()
        for active in anchor_context(anchor, owner):
            if active is None or active[2] != owner:
                result.add(("value", ("binding", owner, name)))
                continue
            token(("anchor", active))
            specs = bindings_by_body.get(owner, {})
            spec = specs.get(name)
            if spec is None:
                continue
            index, path, defaults, rest = spec
            for _, caller, capture, operands in anchors[active]:
                if rest == ((), None):
                    selected = ("args", active, index)
                else:
                    selected = operands[index] if isinstance(operands, Packet) else ("value", ("undefined",))
                    selected = select_path(selected, path, defaults, rest, owner, active)
                result.add(selected)
        return result

    def reference_value(selected, position, anchor):
        return query(("reference", selected, position, anchor), lambda: compute_reference(selected, position, anchor))

    def compute_reference(selected, position, anchor):
        base, dot, suffix = selected.partition(".")
        if dot:
            values, exact = reference_value(base, position, anchor)
            for field in suffix.split("."):
                values, exact = project(values, exact, field, position, anchor)
            return values, exact
        owner = lexical(base, position)
        candidates = variable_events.get((owner, base), ())
        resolved, operand_origins = [], {}
        for event in candidates:
            span, path, defaults, rest, rhs, at, context, certain = event
            write_at = rhs[1] - 1
            possible, _ = ordering(context, position, write_at)
            if not possible:
                continue
            creation_anchors = anchor_context(anchor, function_owner(at)) if function_owner(at) != -1 else (None,)
            values, exact, originals = set(), True, set()
            for creation in creation_anchors:
                item = select_path(recipe(rhs, creation), path, defaults, rest, rhs[1], creation)
                originals.add(item)
                selected_values, known = value_of(item)
                values.update(selected_values)
                exact &= known
            resolved.append((write_at, context, certain, frozenset(values), exact))
            operand_origins.setdefault(resolved[-1], set()).update(originals)
        last = max((event_order(at, position) for at, context, certain, _, exact in resolved if certain and exact and ordering(context, position, at)[1]), default=None)
        values, known, selected_originals = set(), True, set()
        if last is None:
            if (owner, base) in declarations and not (syntax_owner(position) != owners[position] and name_is_parameter(base, owner)):
                values.update(("callable", span, anchor) for span in declarations[owner, base])
                selected_originals.update(("value", value) for value in values)
            elif name_is_parameter(base, owner):
                selected_originals.update(parameter_operands(base, owner, anchor))
                for item in selected_originals:
                    initial, certain = value_of(item)
                    values.update(initial)
                    known &= certain and all(value[0] != "binding" for value in initial)
            elif owner is None:
                initial = roots_global(base)
                if initial == ("global",):
                    values.add(initial)
                else:
                    extra, certain = member_one(("global",), base, position, anchor)
                    if extra:
                        values.update(extra)
                        known &= certain
                    else:
                        values.add(("unknown", base))
                        known = False
                selected_originals.update(("value", value) for value in values)
            else:
                values.add(("binding", owner, base))
                selected_originals.add(("value", ("binding", owner, base)))
                known = False
        for event in resolved:
            at, context, _, selected_values, exact = event
            if last is None or event_order(at, position) >= last or not ordering(context, position, at)[1]:
                values.update(selected_values)
                known &= exact
                selected_originals.update(operand_origins[event])
        key = selected, position, anchor
        if reference_operands.get(key) != selected_originals:
            reference_operands[key] = selected_originals
            invalidate(("reference-operands", key))
        return frozenset(values), known

    def name_is_parameter(name, owner):
        return name in bindings_by_body.get(owner, {})

    def expression(span, anchor):
        return query(("expression", span, anchor), lambda: compute_expression(span, anchor))

    def compute_expression(span, anchor):
        begin, end = trim(*span)
        if begin >= end:
            return undefined
        body = callable_body((begin, end))
        if body is not None:
            body_start, _, _, _, _, params = body
            specs = bindings_by_body.setdefault(body_start, {})
            for index, parameter in enumerate(params):
                targets = []
                list(javascript_binding_names(code, *parameter, _facts=targets, _native=native))
                for target, path, defaults, rest in targets:
                    name = code[slice(*target)].strip()
                    if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                        specs[name] = index, path, defaults, rest
            return frozenset({("callable", (begin, end), anchor)}), True
        if re.match(r"await\b", code[begin:end]):
            return expression((begin + 5, end), anchor)
        selected = role((begin, end))
        if selected is not None:
            values, known = set(), True
            for operand in selected_operands((begin, end), anchor):
                result, exact = expression(operand, anchor)
                values.update(result)
                known &= exact
            return frozenset(values), known
        aggregate = literal((begin, end))
        if aggregate is not None:
            return frozenset({("aggregate", (begin, end), anchor)}), True
        for site in ending_sites.get(end, ()):
            if call_sites[site][0] == begin and call_sites[site][3]:
                return invocation(site, anchor)
        raw = native[begin:end].strip()
        try:
            if re.fullmatch(JS_STRING, raw):
                return frozenset({("string", javascript_string(raw))}), True
        except ValueError:
            pass
        if re.fullmatch(r"(?:\d+(?:\.\d+)?|null|true|false)", raw):
            return data
        if re.fullmatch(r"[A-Za-z_$][\w$]*", code[begin:end]):
            if raw == "this":
                owner = function_owner(begin)
                if owner == -1:
                    return receiver_value(None)
                values, known = set(), True
                for active in anchor_context(anchor, owner):
                    result, exact = receiver_value(active) if active is not None else (frozenset({("unknown", "this")}), False)
                    values.update(result)
                    known &= exact
                return frozenset(values), known
            return reference_value(raw, begin, anchor)
        comma = node_expression_parts(code, begin, end)
        if len(comma) > 1:
            return expression(comma[-1], anchor)
        if code[end - 1:end] == ")" and end - 1 in pairs:
            opening = pairs[end - 1]
            if opening > begin and opening in call_sites:
                return invocation(opening, anchor)
        selected = selected_member(begin, end, anchor, end)
        if selected is not None:
            return selected[0], selected[1]
        # A tag's end can contain masked literal text, so inspect immutable sites.
        for site in ending_sites.get(end, ()):
            if call_sites[site][0] == begin:
                return invocation(site, anchor)
        return frozenset({("unknown", (begin, end), anchor)}), False

    def aggregate_fields(root, position):
        fields, exact = query(("fields", root), lambda: compute_fields(root))
        return fields if isinstance(fields, dict) else {}, exact

    def event_presence(root, event, position):
        at, context, certain, _, _, producer = event
        possible, definite = ordering(context, position, at)
        if not possible:
            return False, False
        if not native_live(event[4]):
            return False, False
        if producer is not None:
            token(producer)
            if producer[0] == "target":
                token(("qualification", producer))
                roots, qualified, unconditional = qualifications.get(producer, (empty, False, False))
                certain = unconditional and qualified and roots == frozenset({root}) and states.get(producer, [None, False])[1]
        return possible, certain and definite

    def deletion(event):
        producer = event[5]
        return ((producer is not None and producer[0] == "evaluated-read" and producer[1][1] == "delete")
                or event in array_removals)

    def copy_presence(root, field, position, anchor):
        if root[0] != "aggregate":
            return field_presence(root, field, position, anchor)
        fields, known = aggregate_fields(root, position)
        states = {(field in fields, field in fields)}
        if not known or field is None:
            states.update({(False, False), (True, True), (True, False)})
        watch_bucket(("effects", root))
        events = [(event, name) for name in (field, None) for event in tuple(effects.get((root, name), ()))
                  if event_presence(root, event, position)[0]]
        for event, name in sorted(events, key=lambda selected: event_order(selected[0][0], position)):
            item = event[3]
            if deletion(event):
                selected = {(False, False)}
            elif item[0] == "descriptor":
                attributes = set()
                if item in direct_accessors:
                    attributes.add(True)
                else:
                    values, exact = value_of(item[1])
                    for value in values:
                        layout, certain = aggregate_fields(value, position) if value[0] == "aggregate" else ({}, False)
                        if not certain:
                            attributes.update({True, False, None})
                        elif "enumerable" in layout:
                            attributes.update(predicate(layout["enumerable"]))
                        else:
                            attributes.add(None)
                    if not exact or not values:
                        attributes.update({True, False, None})
                selected = {(True, enumerable if attribute is None and present else False if attribute is None else attribute)
                            for present, enumerable in states for attribute in attributes}
            else:
                selected = {(True, enumerable if present else True) for present, enumerable in states}
            if name is not None and event_presence(root, event, position)[1]:
                states = selected
            else:
                states.update(selected)
        possible, definite = field_presence(root, field, position, anchor)
        return possible and any(present and enumerable for present, enumerable in states), definite and all(present and enumerable for present, enumerable in states)

    def enumeration_fields(root, position, anchor):
        fields, known = aggregate_fields(root, position)
        names = set(fields)
        watch_bucket(("effects", root))
        for (selected, field), events in tuple(effects.items()):
            if selected != root:
                continue
            for event in tuple(events):
                if not event_presence(root, event, position)[0] or deletion(event):
                    continue
                names.add(field)
                known &= field is not None
        return {name for name in names if name is None or copy_presence(root, name, position, anchor)[0]}, known

    def compute_fields(root):
        span, anchor = root[1:]
        position = span[0]
        literal(span)
        entries = literal_cache[span][1]
        if code[span[0]] == "[":
            selected = array_contributions(root, position, anchor, effects_enabled=False)
            fields = {str(index): item for _, index, item in selected if index is not None}
            remaining = frozenset(item for _, index, item in selected if index is None)
            if field_remainders.get(root) != remaining:
                field_remainders[root] = remaining
                invalidate(("field-remainders", root))
            accessor_fields[root] = {}
            return fields, not remaining
        fields, accessors, known, methods = {}, set(), True, {}
        remaining, delegated = set(), set()
        for name, operand, key_recipe in entries:
            raw = native[slice(*operand)].strip()
            if code[slice(*operand)].lstrip().startswith("..."):
                start = operand[0] + code[slice(*operand)].find("...") + 3
                values, exact = expression((start, operand[1]), anchor)
                known &= exact
                for value in values:
                    if value[0] == "aggregate":
                        shared, certain = enumeration_fields(value, operand[0], anchor)
                        for field in shared:
                            presence = [copy_presence(root, field, operand[0], anchor) for root in values]
                            if field is not None and presence and all(definite for _, definite in presence):
                                fields[field] = ("snapshot-member", recipe((start, operand[1]), anchor), field, operand[0], anchor)
                                accessors.discard(field)
                                methods.pop(field, None)
                            else:
                                delegated.add((recipe((start, operand[1]), anchor), operand[0], anchor))
                        known &= certain
                        watch_bucket(("field-remainders", value))
                        watch_bucket(("field-delegates", value))
                        if field_remainders.get(value) or field_delegates.get(value) or not certain:
                            delegated.add((recipe((start, operand[1]), anchor), operand[0], anchor))
                    else:
                        known = False
                        delegated.add((recipe((start, operand[1]), anchor), operand[0], anchor))
                continue
            # Preserve the existing callable syntax's plain accessor role;
            # the public member reader deliberately keeps original spans.
            accessor = re.match(r"\s*(get|set)\s+([A-Za-z_$][\w$]*)\s*\(", code[slice(*operand)])
            if key_recipe is None and accessor and callable_body(operand) is not None:
                field = accessor[2]
                getter, setter = methods.get(field, (None, None)) if field in accessors else (None, None)
                if accessor[1] == "get":
                    item = recipe(operand, anchor)
                    literal_getters.add(item)
                    # The original accessor start is a finite implicit site.
                    # It is not a lexical call or an application certificate.
                    fields[field] = ("descriptor", item, operand[0], anchor)
                    getter = item
                elif field not in accessors:
                    fields[field] = ("value", ("undefined",))
                if accessor[1] == "set":
                    setter = recipe(operand, anchor)
                methods[field] = getter, setter
                accessors.add(field)
                continue
            if key_recipe is not None:
                names, exact = keys_for(native[slice(*key_recipe)], anchor, operand[0])
                known &= exact
                if not exact:
                    remaining.add(recipe(operand, anchor))
            else:
                names = {name} if name is not None else set()
                known &= name is not None
            for field in names:
                fields[field] = recipe(operand, anchor)
                accessors.discard(field)
                methods.pop(field, None)
        accessor_fields[root] = methods
        for table, values, fact in ((field_remainders, frozenset(remaining), "field-remainders"),
                                    (field_delegates, frozenset(delegated), "field-delegates")):
            if table.get(root) != values:
                table[root] = values
                invalidate((fact, root))
        return fields, known

    def project(values, exact, field, position, anchor):
        result, known = set(), exact
        for root in values:
            selected, certain = query(("member", root, field, position, anchor), lambda root=root: member_one(root, field, position, anchor))
            result.update(selected)
            known &= certain
        return frozenset(result), known

    def descriptor_option(item, layout=False):
        source, _, _ = item[1:]
        if item in direct_accessors:
            return "accessor", direct_accessors[item], True
        if source in literal_getters:
            return "accessor", (source, None), True
        values, exact = value_of(source)
        getters, setters, data_items = set(), set(), set()
        for value in values:
            fields, known = aggregate_fields(value, item[2]) if value[0] == "aggregate" else ({}, False)
            exact &= known
            if "value" in fields:
                data_items.add(fields["value"])
            if "get" in fields:
                getters.add(fields["get"])
            if "set" in fields:
                setters.add(fields["set"])
        if getters or setters:
            return "accessor", (frozenset(getters), frozenset(setters)), exact
        if layout:
            return "values", empty, exact
        selected = set()
        for value in data_items:
            selected.update(value_of(value)[0])
        return "values", frozenset(selected or {("undefined",)}), exact

    def field_presence(root, field, position, anchor, seen=frozenset()):
        if root in seen:
            return True, False
        possible = definite = False
        if root[0] == "aggregate":
            fields, known = aggregate_fields(root, position)
            possible = definite = field in fields
            array = code[root[1][0]] == "["
            if array and numeric_field(field):
                index = int(field)
                for _, selected, item in array_contributions(root, position, anchor):
                    if selected == index:
                        possible = definite = True
                    elif selected is None:
                        possible = True
                        if item[0] == "array-tail" and item[2] is not None:
                            low, _ = packet_length(item[3], len(code) + 1)
                            definite |= item[2] <= index < item[2] + low
                _, high = packet_length(packet(("array-at", root, position, anchor), ("array", root, position, anchor)), len(code) + 1)
                if high is not None and index >= high:
                    possible = definite = False
            elif not array:
                possible |= not known or bool(field_remainders.get(root)) or bool(field_delegates.get(root))
        elif root[0] == "copy":
            token(("copy", root))
            if root in copy_packets:
                lengths = [packet_length(child, len(code) + 1) for child in copy_packets[root]]
                low = min((lo for lo, _ in lengths), default=0)
                high = None if any(hi is None for _, hi in lengths) else max((hi for _, hi in lengths), default=0)
                if numeric_field(field):
                    operands_ = [child[int(field)] for child in copy_packets[root]]
                    holes = operands_ and all(item[0] == "span" and trim(*item[1])[0] == trim(*item[1])[1] for item in operands_)
                    possible, definite = not holes and (high is None or int(field) < high), not holes and int(field) < low
                else:
                    possible, definite = field is None and high != 0, False
            else:
                entries = [field_presence(value, field, root[1], root[2], seen | {root})
                           for source in copy_sources.get(root, ()) for value in value_of(source)[0]]
                possible = any(entry[0] for entry in entries)
                definite = bool(entries) and all(entry[1] for entry in entries)
        else:
            return True, False
        watch_bucket(("effects", root))
        eligible = [(event, *event_presence(root, event, position))
                    for name in (field, None) for event in tuple(effects.get((root, name), ()))]
        prior = max((event_order(event[0], position) for event, possible, certain in eligible
                     if possible and certain and not deletion(event)), default=None)
        definite |= prior is not None
        for event, admitted, certain in eligible:
            if not admitted:
                continue
            if deletion(event):
                if prior is None or event_order(event[0], position) >= prior or not ordering(event[1], position, event[0])[1]:
                    definite = False
            else:
                possible = True
        return possible, definite

    def property_options(root, field, position, anchor, inherited=True, layout=False):
        options = []
        originals = {}
        unordered = set()
        own_absent = None
        tag = "string" if raw_result(root) else root[0]
        def add(at, certain, item, known=True, accessor=None):
            if accessor is not None:
                kind, selected, exact = "accessor", accessor, known
            elif item[0] == "descriptor":
                kind, selected, exact = descriptor_option(item, layout)
            elif layout:
                kind, selected, exact = "values", empty, True
            else:
                selected, exact = value_of(item)
                kind = "values"
            originals[len(options)] = item
            options.append((at, certain, kind, selected, known and exact))
        if layout and tag in {"copy", "args", "rest"}:
            options.append((-1, True, "values", empty, True))
        elif tag == "aggregate":
            array = code[root[1][0]] == "["
            if array and field is None:
                for _, _, item in array_contributions(root, position, anchor):
                    add(-1, False, item)
            elif array and numeric_field(field):
                selected = literal_packet(root)
                _, known = query(("packet-select", selected.key, int(field)), lambda: packet_selection(selected, int(field)))
                add(-1, True, selected[int(field)], known)
            else:
                fields, known = aggregate_fields(root, position)
                if field is None:
                    for name in fields:
                        add(-1, False, ("snapshot-member", ("value", root), name, position, anchor), known)
                elif field in fields:
                    add(-1, True, fields[field], known, accessor_fields.get(root, {}).get(field))
                else:
                    own_absent = known
            if not array:
                watch_bucket(("field-remainders", root))
                for item in field_remainders.get(root, ()):
                    add(-1, False, item, True if layout else False)
                watch_bucket(("field-delegates", root))
                for source, at, capture in field_delegates.get(root, ()):
                    values, certain = value_of(source)
                    present = [copy_presence(value, field, at, capture) for value in values]
                    if field is not None and present and not any(possible for possible, _ in present):
                        continue
                    covered = certain and bool(present) and all(definite for _, definite in present)
                    add(at, covered, ("snapshot-member", source, field, at, capture))
        elif tag == "copy":
            if field is None:
                for _, _, item in array_contributions(root, position, anchor):
                    add(-1, False, item)
            elif numeric_field(field):
                token(("copy", root))
                if root in copy_packets:
                    for child in copy_packets[root]:
                        add(-1, len(copy_packets[root]) == 1, child[int(field)])
                else:
                    values, exact = set(), True
                    for source in copy_sources.get(root, ()):
                        selected, certain = value_of(("snapshot-member", source, field, root[1], root[2]))
                        values.update(selected)
                        exact &= certain
                    options.append((-1, True, "values", frozenset(values), exact))
            else:
                own_absent = True
        elif tag == "args":
            token(("anchor", root[1]))
            if numeric_field(field):
                values, exact = empty, True
                if anchors[root[1]]:
                    values, exact = value_of(packet(("args", root[1], root[2]), ("args", root[1], root[2]))[int(field)])
                options.append((-1, True, "values", values, exact))
        elif tag == "rest":
            item = root[1]
            source, path, defaults, rest, at, creation = item[1:]
            selected, exact = value_of(("path", source, path, defaults, None, at, creation))
            offset = rest[1]
            target_field = str(int(field) + int(offset)) if offset is not None and str(offset).isdigit() and numeric_field(field) else field
            values, known = project(selected, exact, target_field, position, creation)
            options.append((-1, True, "values", values, known))
        elif root == ("global",) and inherited:
            value = roots_global(field)
            if value is not None:
                options.append((-1, True, "values", frozenset({value}), True))
            elif not layout and field is not None:
                add(-1, True, ("value", ("unknown", position, field, anchor)), False)
        elif root == ("intrinsic", "String") and field == "raw":
            options.append((-1, True, "values", frozenset({("intrinsic", "String.raw")}), True))
        elif tag == "intrinsic" and root[1] in {"Object", "Reflect"}:
            allowed = {"assign", "defineProperty", "defineProperties"} if root[1] == "Object" else {"apply", "construct", "get", "set", "defineProperty"}
            if field in allowed:
                options.append((-1, True, "values", frozenset({("intrinsic", root[1] + "." + field)}), True))
        if field in {"slice", "forEach", "at", "concat", "copyWithin", "entries", "every", "fill", "filter", "find", "findIndex", "findLast", "findLastIndex", "flat", "flatMap", "includes", "indexOf", "join", "keys", "lastIndexOf", "map", "pop", "push", "reduce", "reduceRight", "reverse", "shift", "some", "sort", "splice", "toLocaleString", "toReversed", "toSorted", "toSpliced", "toString", "unshift", "values", "with"} and not options and (tag == "copy" or tag == "aggregate" and code[root[1][0]] == "["):
            options.append((-1, True, "values", frozenset({("array-method", field)}), True))
        if field in {"__defineGetter__", "__defineSetter__"} and not options and tag == "aggregate":
            options.append((-1, True, "values", frozenset({("intrinsic", field)}), True))
        if field in {"call", "apply", "bind"} and not options and tag in {"handle", "callable", "bound", "intrinsic", "method", "array-method", "read-callable", "unknown"}:
            options.append((-1, True, "values", frozenset({("method", field)}), tag != "unknown"))
        if field == "constructor" and not options:
            callable_ = tag in {"handle", "callable", "bound", "intrinsic", "method", "array-method", "read-callable", "callable-data"}
            constructor = ("handle", "constructor") if callable_ or tag in {"unknown", "binding"} else ("callable-data",)
            options.append((-1, True, "values", frozenset({constructor}), tag not in {"unknown", "binding"}))
        if not options and own_absent is not None:
            options.append((-1, True, "values", frozenset({("undefined",)}) if own_absent else frozenset({("unknown",)}), own_absent))
        # Bucket growth wakes this query, but only eligible events below are
        # causal predecessors. Importing all historical producers here would
        # make a Set depend on its own excluded, later publication.
        fact = ("effects", root, field)
        watch_bucket(fact)
        watch_bucket(("effects", root, None))
        if field is None:
            watch_bucket(("effects", root))
        selected_events = tuple(event for (selected, name), events in effects.items() if selected == root for event in events) if field is None else tuple(effects.get((root, field), ()))
        event_fields = {event: name for (selected, name), events in effects.items() if selected == root for event in events} if field is None else {}
        wildcard_events = tuple(effects.get((root, None), ())) if field is not None else ()
        for event in (*selected_events, *wildcard_events):
            at, context, certain, item, effect_anchor, producer = event
            possible, definite = event_presence(root, event, position)
            if not possible:
                continue
            # An ordinary Set preserves the descriptor role. Selecting a Set
            # operation therefore does not consume prior Set values, including
            # legitimate loop backedges; only Define can replace that role.
            if layout and item[0] != "descriptor":
                continue
            delegated = item[1] if item[0] == "descriptor" else item
            if event in wildcard_events and delegated[0] == "snapshot-member" and delegated[2] is None:
                values, _ = value_of(delegated[1])
                if values and not any(field_presence(value, field, delegated[3], delegated[4])[0] for value in values):
                    continue
                projected = ("snapshot-member", delegated[1], field, delegated[3], delegated[4])
                item = ("descriptor", projected, item[2], item[3]) if item[0] == "descriptor" else projected
            event_field = None if event in wildcard_events else event_fields.get(event, field)
            for origin in tuple(effect_origins.get((root, event_field, event), ())):
                token(origin)
            if function_owner(write_sites.get(at, at)) != function_owner(position):
                unordered.add(at)
            add(at, definite and field is not None and event not in wildcard_events, item)
        fact = ("escape", root)
        watch_bucket(fact)
        user_global = root != ("global",) or any(ordering(event[1], position, event[0])[0]
            for name in (field, None) for event in tuple(effects.get((root, name), ())))
        for event in tuple(escapes.get(root, ())) if user_global else ():
            at, context, origin, _ = event
            possible, definite = ordering(context, position, at)
            if possible:
                for producer in tuple(relation_origins.get((fact, event), ())):
                    token(producer)
                value = ("unknown", position, field, anchor)
                grow(affected, value, (origin,), ("affected", value))
                options.append((at, False, "values", frozenset({value}), False))
        origins = affected_at(root, position, anchor) if user_global else ()
        if origins:
            value = ("unknown", position, field, anchor)
            grow(affected, value, origins, ("affected", value))
            options.append((-1, False, "values", frozenset({value}), False))
        last = max((event_order(at, position) for at, certain, _, _, exact in options if certain and exact), default=None)
        if not layout and field is not None:
            key = root, field, position, anchor
            replaced = last is not None and last[0] >= 0
            if replacements.get(key) != replaced:
                replacements[key] = replaced
                invalidate(("replacement", *key))
        selected_options = []
        selected_originals = set()
        for index, (at, _, kind, selected, certain) in enumerate(options):
            if last is None or event_order(at, position) >= last or at in unordered:
                selected_options.append((kind, selected, certain))
                if index in originals:
                    selected_originals.add(originals[index])
                else:
                    selected_originals.update(("value", value) for value in selected if kind == "values")
        key = root, field, position, anchor, layout
        if member_operands.get(key) != selected_originals:
            member_operands[key] = selected_originals
            invalidate(("member-operands", key))
        return selected_options

    def invoke_accessor(methods, root, position, anchor, actual=()):
        values, exact = set(), True
        methods = (methods,) if isinstance(methods, tuple) and methods and methods[0] == "span" else methods or ()
        for item in methods:
            targets, known = value_of(item)
            site = item[1][0] if item[0] == "span" else position
            implicit_sites.setdefault(site, (position, position))
            selected, certain = invoke_values(targets, ("value", root), actual, site, anchor, set())
            values.update(selected)
            exact &= known and certain
        return frozenset(values or {("undefined",)}), exact

    def member_one(root, field, position, anchor, inherited=True):
        if root[0] == "binding" and plain_const_binding(root[1], root[2]):
            return frozenset({root}), False
        options = property_options(root, field, position, anchor, inherited)
        if not options:
            return frozenset({("unknown", position, field, anchor)}), False
        values, exact = set(), True
        for kind, selected, certain in options:
            if kind == "accessor":
                result, known = invoke_accessor(selected[0], root, position, anchor)
                values.update(result)
                exact &= known
            else:
                values.update(selected)
            exact &= certain
        return frozenset(values), exact

    def set_property(root, field, item, at, context, certain, anchor, producer=None):
        if root[0] in {"undefined", "data", "string"} or raw_result(root):
            return
        options = property_options(root, field, write_sites.get(at, at), anchor, layout=True)
        data_option = not options
        for kind, selected, known in options:
            if kind == "accessor":
                actual = packet((at, anchor, root, field, "setter", item), ("sequence", ((item, False),)))
                invoke_accessor(selected[1], root, at, anchor, actual)
            else:
                data_option = True
        if data_option:
            publish(root, field, item, at, context, certain and all(kind != "accessor" and known for kind, _, known in options), anchor, producer)
        if any(ordering(escaped_context, at, escaped_at)[0] for escaped_at, escaped_context, _, _ in tuple(escapes.get(root, ()))):
            implicit_sites.setdefault(at, (at, at))
            actual = packet((at, anchor, root, field, "opaque-setter"), ("sequence", ((item, False),)))
            observe_global(at, anchor)
            opaque_call(("value", root), actual, at, anchor, set(), retain_receiver=root != ("global",))

    def publish(root, field, item, at, context, certain, anchor, producer=None):
        if root[0] in {"undefined", "data", "string"} or raw_result(root):
            return
        event = at, context, certain, item, anchor, producer
        key = root, field
        selected = effects.setdefault(key, set())
        origin = stack[-1] if stack else None
        fact = ("effects", root, field)
        origins = effect_origins.setdefault((root, field, event), set())
        new_producer = origin is not None and origin not in origins
        if origin is not None:
            origins.add(origin)
        if event not in selected or new_producer:
            selected.add(event)
            invalidate(fact)
            invalidate(("effects", root))

    # Packet descriptors are structural graph edges, never runtime values or
    # expanded actual environments. Their leaves remain ordinary recipes.
    packet_nodes = {}

    class Packet:
        def __init__(self, key, form):
            self.key = key
            self.form = form

        def __hash__(self):
            return hash(self.key)

        def __eq__(self, other):
            return isinstance(other, Packet) and self.key == other.key

        def __bool__(self):
            return packet_length(self, 1)[1] != 0

        def __len__(self):
            # Adapter consumers ask only whether positions 0, 1 and 2 exist.
            # This saturated abstract length is not a materialization limit.
            limit = 3 + sum(spec[0] for specs in bindings_by_body.values() for spec in specs.values()) + 2 * len(call_sites)
            low, high = packet_length(self, limit)
            return high if high is not None else limit

        def __getitem__(self, index):
            if isinstance(index, slice):
                raise TypeError("packet slices need a lexical adapter identity")
            items, certain = query(("packet-select", self.key, index),
                lambda: packet_selection(self, index))
            if len(items) == 1:
                return next(iter(items))
            selected = ("choice", ("packet-select", self.key, index), "operand")
            options = choices.setdefault(selected, set())
            before = len(options)
            options.update(items)
            if len(options) != before:
                invalidate(("choices", selected))
            return selected

        def __iter__(self):
            return iter(packet_leaves(self))

    def packet(key, form):
        result = packet_nodes.get(key)
        if result is None:
            result = packet_nodes[key] = Packet(key, form)
        elif result.form != form:
            raise ValueError("one lexical packet node has conflicting definitions")
        return result

    def packet_view(actual, offset, site, anchor, adapting, role):
        key = (site, anchor, frozenset(adapting), role, actual.key if isinstance(actual, Packet) else actual, offset)
        return packet(key, ("drop", actual, offset))

    def packet_join(left, right, site, anchor, adapting):
        return packet((site, anchor, frozenset(adapting), "bound-prefix", left.key, right.key if isinstance(right, Packet) else right),
            ("join", left, right))

    def literal_packet(root):
        literal(root[1])
        pieces = []
        for _, span, _ in literal_cache[root[1]][1]:
            begin, end = trim(*span)
            spread = code[begin:begin + 3] == "..."
            pieces.append((recipe((begin + 3, end) if spread else span, root[2]), spread))
        return packet(root, ("sequence", tuple(pieces)))

    def spread_packets(item, position=None, capture=None):
        values, exact = value_of(item)
        if position is None:
            position = item[1][0] if item[0] == "span" else item[3] if item[0] == "snapshot-member" else 0
            capture = item[2] if item[0] == "span" else item[4] if item[0] == "snapshot-member" else capture
        structural = False
        if item[0] == "span":
            begin, end = trim(*item[1])
            name = code[begin:end]
            owner = lexical(name, begin) if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else None
            spec = bindings_by_body.get(owner, {}).get(name)
            if spec is not None and spec[3] == ((), None) and not variable_events.get((owner, name)):
                contexts = anchor_context(item[2], owner)
                structural = bool(values) and all(root[0] == "args" and root[1] in contexts
                    and root[1] is not None and root[1][2] == owner and root[2] == spec[0]
                    for root in values)
        selected, opaque = [], not (exact or structural)
        for root in values:
            if root[0] == "args":
                selected.append(packet(root, ("args", root[1], root[2])))
            elif root[0] == "copy" or root[0] == "aggregate" and code[root[1][0]] == "[":
                selected.append(packet(("array-at", root, position, capture), ("array", root, position, capture)))
            else:
                opaque = True
        return tuple(selected), opaque

    def array_length(root, position, anchor, limit, constraints, active, memo):
        if root[0] == "aggregate":
            low, high = packet_length(literal_packet(root), limit, constraints, active, memo)
        else:
            token(("copy", root))
            children = copy_packets[root] if root in copy_packets else tuple(
                child for source in copy_sources.get(root, ()) for child in spread_packets(source, root[1], root[2])[0])
            lengths = [packet_length(child, limit, constraints, active, memo) for child in children]
            low = min((entry[0] for entry in lengths), default=0)
            high = None if not lengths or any(entry[1] is None for entry in lengths) else max(entry[1] for entry in lengths)
        watch_bucket(("effects", root))
        lengths = [(event, array_lengths[event]) for event in tuple(effects.get((root, "length"), ()))
                   if event in array_lengths and event_presence(root, event, position)[1]]
        last = max((event_order(event[0], position) for event, _ in lengths), default=None)
        if last is not None:
            selected_lengths = [length for event, length in lengths if event_order(event[0], position) == last]
            low = min(limit, min(selected_lengths))
            high = None if max(selected_lengths) > limit else max(selected_lengths)
        for (selected, field), events in tuple(effects.items()):
            if selected != root:
                continue
            eligible = [event for event in tuple(events) if event_presence(root, event, position)[0]
                        and (last is None or event_order(event[0], position) > last)]
            if not eligible:
                continue
            if numeric_field(field):
                index = int(field) + 1
                high = None if high is None or index > limit else max(high, index)
                member_one(root, field, position, anchor)
                token(("replacement", root, field, position, anchor))
                if replacements.get((root, field, position, anchor), False):
                    low = max(low, min(limit, index))
            elif field is None or field == "length":
                high = None
        return low, high

    def tail_operands(root, start, child, position, anchor):
        def compute():
            low, high = packet_length(child, len(code) + 1)
            fields = []
            watch_bucket(("effects", root))
            if start is not None and high is not None and low == high:
                for selected, field in tuple(effects):
                    if selected != root or not numeric_field(field) or not start <= int(field) < start + high:
                        continue
                    member_one(root, field, position, anchor)
                    token(("replacement", root, field, position, anchor))
                    if replacements.get((root, field, position, anchor), False):
                        fields.append(field)
                if len(set(fields)) == high:
                    return frozenset(("snapshot-member", ("value", root), field, position, anchor) for field in fields), True
            return frozenset(packet_leaves(child)), False
        return query(("array-tail", root, start, child.key, position, anchor), compute)[0]

    def array_contributions(root, position, anchor, *, holes=False, effects_enabled=True):
        key = "array-contributions", root, position, anchor, holes, effects_enabled
        def compute():
            result, visited = set(), set()
            def add(site, index, item):
                selected = ("snapshot-member", ("value", root), str(index), position, anchor) if index is not None else item
                result.add((site, index, selected))
            def tail(site, offset, child):
                result.add((site, None, ("array-tail", root, offset, child, position, anchor)))
            def walk(node, offset=0, spreading=False):
                if node.key in visited:
                    tail(position, offset, node)
                    return
                visited.add(node.key)
                kind, *parts = node.form
                if kind == "array":
                    selected, at, capture = parts
                    for site, index, item in array_contributions(selected, at, capture, holes=holes or spreading):
                        if offset is not None and index is not None:
                            add(site, offset + index, item)
                        elif item[0] == "array-tail":
                            _, _, inner, child, _, _ = item
                            tail(site, None if offset is None or inner is None else offset + inner, child)
                        else:
                            child = packet(("array-contribution", item), ("sequence", ((item, False),)))
                            tail(site, None, child)
                    return
                if kind != "sequence":
                    for item in packet_leaves(node):
                        result.add((position, None, item))
                    return
                for item, spread in parts[0]:
                    site = item[1][0] if item[0] == "span" else item[1][3][1] if item[0] == "choice" and item[1][0] == "native-result" else position
                    if not spread:
                        empty_part = item[0] == "span" and trim(*item[1])[0] == trim(*item[1])[1]
                        if holes or spreading or not empty_part:
                            if offset is not None:
                                add(site, offset, item)
                            else:
                                tail(site, None, packet(("array-contribution", item), ("sequence", ((item, False),))))
                        offset = None if offset is None else offset + 1
                        continue
                    children, opaque = spread_packets(item)
                    lengths = []
                    for child in children:
                        walk(child, offset, True)
                        lengths.append(packet_length(child, len(code) + 1))
                    if opaque:
                        tail(site, None, packet(("opaque-array-contribution", item), ("sequence", ((item, False),))))
                    if opaque or not lengths or any(high is None or low != high for low, high in lengths) or len({high for _, high in lengths}) != 1:
                        offset = None
                    elif offset is not None:
                        offset += lengths[0][1]
            if root[0] == "aggregate":
                walk(literal_packet(root))
            elif root[0] == "copy":
                token(("copy", root))
                if root in copy_packets:
                    for child in copy_packets[root]:
                        walk(child)
                else:
                    for source in copy_sources.get(root, ()):
                        children, opaque = spread_packets(source, root[1], root[2])
                        for child in children:
                            walk(child)
                        if opaque:
                            result.add((root[1], None, source))
            else:
                result.add((position, None, ("value", ("unknown",))))
            if effects_enabled:
                watch_bucket(("effects", root))
                for (selected, field), events in tuple(effects.items()):
                    if selected != root:
                        continue
                    for event in events:
                        if ordering(event[1], position, event[0])[0] and (field is None or numeric_field(field)):
                            add(event[0], int(field) if field is not None else None,
                                ("snapshot-member", ("value", root), field, position, anchor))
                _, high = array_length(root, position, anchor, len(code) + 1, {}, frozenset(), {})
                if high is not None:
                    result = {entry for entry in result if entry[1] is None or entry[1] < high}
            return frozenset(result), False
        return query(key, compute)[0]

    def packet_edges(actual, constraints):
        form = actual.form
        if form[0] != "args":
            return ()
        active = form[1]
        if active[0] == "bind-capture":
            _, site, anchor = active
            token(("bind", site, anchor))
            selected = tuple((site, anchor, source, actual) for source, actual in tuple(bind_sites.get((site, anchor), ())))
            if active in constraints:
                selected = tuple(edge for edge in selected if edge == constraints[active])
            return selected
        token(("anchor", active))
        selected = anchors.get(active, ())
        if active in constraints:
            selected = tuple(edge for edge in selected if edge == constraints[active])
        return tuple(selected)

    def packet_length(actual, limit, constraints=None, active=frozenset(), memo=None):
        if not isinstance(actual, Packet):
            return len(actual), len(actual)
        constraints = {} if constraints is None else constraints
        memo = {} if memo is None else memo
        if actual.key in active:
            return 0, None
        if actual.key in memo:
            return memo[actual.key]
        active = active | {actual.key}
        kind, *parts = actual.form
        def length(child):
            return packet_length(child, limit, constraints, active, memo)
        def add(left, right):
            lo = min(limit, left[0] + right[0])
            total = None if left[1] is None or right[1] is None else left[1] + right[1]
            hi = None if total is None or total > limit else total
            return lo, hi
        if kind == "array":
            result = array_length(parts[0], parts[1], parts[2], limit, constraints, active, memo)
        elif kind == "sequence":
            result = (0, 0)
            for item, spread in parts[0]:
                if not spread:
                    result = add(result, (1, 1))
                    continue
                candidates, opaque = spread_packets(item)
                lengths = [length(child) for child in candidates]
                low = min((entry[0] for entry in lengths), default=0)
                high = None if opaque or not lengths or any(entry[1] is None for entry in lengths) else max(entry[1] for entry in lengths)
                result = add(result, (low, high))
        elif kind == "drop":
            lo, hi = length(parts[0])
            result = max(0, lo - parts[1]), None if hi is None else max(0, hi - parts[1])
        elif kind == "join":
            result = add(length(parts[0]), length(parts[1]))
        else:
            lengths = []
            for edge in packet_edges(actual, constraints):
                lo, hi = length(edge[3])
                lengths.append((max(0, lo - parts[1]), None if hi is None else max(0, hi - parts[1])))
            result = (min((entry[0] for entry in lengths), default=0),
                None if not lengths or any(entry[1] is None for entry in lengths) else max(entry[1] for entry in lengths))
        memo[actual.key] = result
        return result

    def packet_leaves(actual, visited=None):
        if visited is None and isinstance(actual, Packet):
            leaves, _ = query(("packet-leaves", actual.key),
                lambda: (frozenset(packet_leaves(actual, set())), False))
            return set(leaves)
        visited = set() if visited is None else visited
        if not isinstance(actual, Packet):
            return set(actual)
        if actual.key in visited:
            return set()
        visited.add(actual.key)
        kind, *parts = actual.form
        result = set()
        if kind == "array":
            result.update(item for _, _, item in array_contributions(parts[0], parts[1], parts[2], holes=True))
        elif kind == "sequence":
            for item, spread in parts[0]:
                result.add(item)
                if spread:
                    for child in spread_packets(item)[0]:
                        result.update(packet_leaves(child, visited))
        elif kind == "args":
            for edge in packet_edges(actual, {}):
                result.update(packet_leaves(edge[3], visited))
        else:
            for child in parts[:2] if kind == "join" else parts[:1]:
                result.update(packet_leaves(child, visited))
        return result

    def packet_selection(actual, index, constraints=None, *, variants=False):
        # Rest offsets never enter query keys. Cut repeated nodes before
        # offset arithmetic; widen ambiguity before forming choice products.
        constraints = {} if constraints is None else dict(constraints)
        offsets = sum(spec[0] for specs in bindings_by_body.values() for spec in specs.values())
        limit = index + offsets + 2 * len(call_sites) + 1
        visited, cache = {}, {}
        unknown = ("value", ("unknown",))
        def opaque(node, selected):
            return [(item, selected) for item in packet_leaves(node) | {unknown}], False
        def walk(node, at, selected, active):
            if not isinstance(node, Packet):
                return [((node[at] if at < len(node) else ("value", ("undefined",))), selected)], True
            if node.key in active:
                return opaque(node, selected)
            previous = visited.get(node.key)
            if previous is not None and previous != at:
                return opaque(node, selected)
            visited[node.key] = at
            active = active | {node.key}
            kind, *parts = node.form
            if kind == "array":
                item = ("snapshot-member", ("value", parts[0]), str(at), parts[1], parts[2])
                return [(item, selected)], True
            if kind == "args":
                edges = packet_edges(node, selected)
                if len(edges) > 1 and selected and parts[0] not in selected:
                    return opaque(node, selected)
                result, exact = [], bool(edges)
                for edge in edges:
                    branch = {**selected, parts[0]: edge}
                    values, certain = walk(edge[3], at + parts[1], branch, active)
                    result.extend(values)
                    exact &= certain
                return result or [(unknown, selected)], exact
            if kind == "drop":
                return walk(parts[0], at + parts[1], selected, active)
            if kind == "join":
                children = (parts[0], parts[1])
            else:
                def remaining():
                    for item, spread in parts[0]:
                        if not spread:
                            yield item
                            continue
                        values, uncertain = spread_packets(item)
                        if uncertain or len(values) != 1:
                            yield None
                            return
                        yield values[0]
                children = remaining()
            for child in children:
                if child is None:
                    return opaque(node, selected)
                if not isinstance(child, Packet):
                    if at == 0:
                        return [(child, selected)], True
                    at -= 1
                    continue
                low, high = packet_length(child, limit, selected, memo=cache)
                if high is not None and low == high and high <= at:
                    at -= high
                elif low > at:
                    return walk(child, at, selected, active)
                else:
                    return opaque(node, selected)
            return [(("value", ("undefined",)), selected)], True
        selected, exact = walk(actual, index, constraints, frozenset())
        return (selected, exact) if variants else (frozenset(item for item, _ in selected), exact)

    def operands(parts, anchor, site):
        selected = []
        for span in parts:
            if span is None:
                selected.append((recipe(None, anchor), False))
                continue
            begin, end = trim(*span)
            spread = code[begin:begin + 3] == "..."
            selected.append((recipe((begin + 3, end) if spread else span, anchor), spread))
        return packet((site, anchor, "raw"), ("sequence", tuple(selected)))

    def reference_call(span, anchor):
        begin, end = trim(*span)
        if len(node_expression_parts(code, begin, end)) > 1:
            # A comma obtains a value; it does not retain the last property's
            # invocation receiver. Parentheses around a member still do.
            target, known = expression((begin, end), anchor)
            return target, known, ("value", ("undefined",))
        selected = selected_member(begin, end, anchor, end)
        if selected is not None:
            return selected
        target, known = expression((begin, end), anchor)
        return target, known, ("value", ("undefined",))

    def array_operands(item):
        selected, opaque = spread_packets(item)
        if selected:
            for actual in selected:
                # Native apply reads the array-like arguments before the
                # target runs, even when it does not consume its formals.
                for operand in packet_leaves(actual):
                    value_of(operand)
                yield actual
        if opaque or not selected:
            yield packet(("opaque-array", item), ("sequence", ((item, False),)))

    def source_operand(item, constraints=None, seen=frozenset()):
        constraints = {} if constraints is None else constraints
        if item in seen:
            return [], False
        if item[0] == "choice":
            token(("choices", item))
            options = choices.get(item, ())
            if len(options) != 1:
                return [], False
            return source_operand(next(iter(options)), constraints, seen | {item})
        if item[0] == "path":
            source, path, defaults, rest, position, creation = item[1:]
            if not path and rest is None:
                values, certain = value_of(source)
                if values <= frozenset({("undefined",)}) and defaults:
                    return source_operand(recipe(defaults[-1][1], creation), constraints, seen | {item})
                return source_operand(source, constraints, seen | {item})
            return [], False
        if item[0] in {"snapshot-member", "descriptor"}:
            result, exact = [], True
            for source in original_operands(item):
                variants, known = source_operand(source, constraints, seen | {item})
                result.extend(variants)
                exact &= known
            return result, exact and bool(result)
        if item[0] != "span":
            return [], item == ("value", ("undefined",))
        span, creation = item[1:]
        begin, end = trim(*span)
        name = code[begin:end]
        owner = lexical(name, begin) if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else None
        if not name_is_parameter(name, owner):
            if member_span(begin, end) is not None:
                originals = original_operands(item)
                if originals:
                    result, exact = [], True
                    for source in originals:
                        variants, known = source_operand(source, constraints, seen | {item})
                        result.extend(variants)
                        exact &= known
                    return result, exact and bool(result)
            if any(call_sites[site][0] == begin for site in ending_sites.get(end, ())) and original_operands(item):
                # A returned call expression is not a literal source leaf.
                # Preserve its original expression and returned recipes in
                # the existing opaque closure instead of certifying a subset.
                return [], False
            return [((span,), constraints)], True
        result, exact = [], True
        index, path, defaults, rest = bindings_by_body[owner][name]
        for active in anchor_context(creation, owner):
            if active is None or active[2] != owner or rest is not None:
                exact = False
                continue
            token(("anchor", active))
            edges = tuple(anchors[active])
            if active in constraints:
                edges = tuple(edge for edge in edges if edge == constraints[active])
            if len(edges) > 1 and constraints and active not in constraints:
                return [], False
            for edge in edges:
                selected, certain = packet_selection(edge[3], index,
                    {**constraints, active: edge}, variants=True)
                exact &= certain
                for operand, branch in selected:
                    operand = select_path(operand, path, defaults, rest, owner, active)
                    variants, known = source_operand(operand, branch, seen | {item})
                    result.extend(variants)
                    exact &= known
        return result or [((span,), constraints)], exact and bool(result)

    def source_layout(actual, constraints, active=frozenset(), used=None):
        # Certify output size BEFORE expanding a constructor tuple. Repeated
        # subtrees and independent choice products use opaque source closure.
        used = set() if used is None else used
        if not isinstance(actual, Packet):
            return len(actual)
        if actual.key in active or actual.key in used:
            return None
        used.add(actual.key)
        active = active | {actual.key}
        kind, *parts = actual.form
        if kind == "array":
            low, high = packet_length(actual, len(code) + 1, constraints)
            return high if high is not None and low == high else None
        if kind == "args":
            edges = packet_edges(actual, constraints)
            if len(edges) > 1 and constraints and parts[0] not in constraints:
                return None
            lengths = []
            for edge in edges:
                value = source_layout(edge[3], {**constraints, parts[0]: edge}, active, set(used))
                if value is None:
                    return None
                lengths.append(max(0, value - parts[1]))
            return lengths[0] if lengths and len(set(lengths)) == 1 else None
        if kind == "drop":
            value = source_layout(parts[0], constraints, active, used)
            return None if value is None else max(0, value - parts[1])
        children = parts if kind == "join" else []
        if kind == "sequence":
            for item, spread in parts[0]:
                if not spread:
                    children.append(None)
                else:
                    selected, opaque = spread_packets(item)
                    if opaque or len(selected) != 1:
                        return None
                    children.append(selected[0])
        count = 0
        for child in children:
            value = 1 if child is None else source_layout(child, constraints, active, used)
            if value is None:
                return None
            count += value
        return count

    def source_variants(actual, constraints=None, active=frozenset(), used=None):
        constraints = {} if constraints is None else constraints
        used = set() if used is None else used
        if not isinstance(actual, Packet):
            return [((), constraints)], not actual
        if actual.key in active or actual.key in used:
            return [], False
        used.add(actual.key)
        active = active | {actual.key}
        kind, *parts = actual.form
        if kind == "array":
            high = source_layout(actual, constraints)
            if high is None:
                return [], False
            result = [((), constraints)]
            for index in range(high):
                next_result = []
                for spans, branch in result:
                    variants, exact = source_operand(actual[index], branch)
                    if not exact:
                        return [], False
                    next_result.extend((spans + tail, selected) for tail, selected in variants)
                result = next_result
            return result, True
        if kind == "args":
            edges = packet_edges(actual, constraints)
            if len(edges) > 1 and constraints and parts[0] not in constraints:
                return [], False
            result, exact = [], bool(edges)
            for edge in edges:
                branch = {**constraints, parts[0]: edge}
                dropped = packet((actual.key, edge[0], edge[1], "source-rest", edge[3].key if isinstance(edge[3], Packet) else edge[3]),
                    ("drop", edge[3], parts[1]))
                variants, known = source_variants(dropped, branch, active, set(used))
                result.extend(variants)
                exact &= known
            return result, exact
        if kind == "drop":
            high = source_layout(actual, constraints)
            if high is None:
                return [], False
            result = [((), constraints)]
            for index in range(high):
                next_result = []
                for spans, branch in result:
                    items, exact = packet_selection(actual, index, branch, variants=True)
                    if not exact:
                        return [], False
                    for item, selected in items:
                        variants, known = source_operand(item, selected)
                        if not known:
                            return [], False
                        next_result.extend((spans + tail, candidate) for tail, candidate in variants)
                result = next_result
            return result, True
        if kind == "join":
            children = (parts[0], parts[1])
        else:
            children = []
            for item, spread in parts[0]:
                if spread:
                    selected, opaque = spread_packets(item)
                    if opaque or len(selected) != 1:
                        return [], False
                    children.append(selected[0])
                else:
                    children.append(item)
        result = [((), constraints)]
        for child in children:
            next_result = []
            for spans, branch in result:
                if isinstance(child, Packet):
                    variants, known = source_variants(child, branch, active, used)
                else:
                    variants, known = source_operand(child, branch)
                if not known:
                    return [], False
                next_result.extend((spans + tail, selected) for tail, selected in variants)
            result = next_result
        return result, True

    def opaque_sources(actual):
        result, visited = set(), set()
        pending = list(packet_leaves(actual))
        while pending:
            item = pending.pop()
            if item in visited:
                continue
            visited.add(item)
            pending.extend(original_operands(item))
            if item[0] == "choice":
                pending.extend(choices.get(item, ()))
            elif item[0] == "path":
                pending.append(item[1])
                pending.extend(recipe(span, item[-1]) for _, span in item[3])
            elif item[0] == "span":
                span, creation = item[1:]
                result.add(span)
                begin, end = trim(*span)
                name = code[begin:end]
                owner = lexical(name, begin) if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else None
                if name_is_parameter(name, owner):
                    for active in anchor_context(creation, owner):
                        if active is not None and active[2] == owner:
                            token(("anchor", active))
                            for edge in anchors[active]:
                                pending.extend(packet_leaves(edge[3]))
        return result

    def original_operands(item):
        result = set()
        if item[0] == "array-tail":
            result.update(tail_operands(*item[1:]))
        elif item[0] == "snapshot-member":
            source, field, site, anchor = item[1:]
            values, _ = value_of(source)
            for root in values:
                member_one(root, field, site, anchor)
                key = root, field, site, anchor, False
                token(("member-operands", key))
                result.update(member_operands.get(key, ()))
        elif item[0] == "descriptor":
            source, site, anchor = item[1:]
            for root in value_of(source)[0]:
                if root[0] == "aggregate":
                    fields, _ = aggregate_fields(root, site)
                    result.update(fields[field] for field in ("value", "get") if field in fields)
        elif item[0] == "span":
            values, _ = value_of(item)
            span, anchor = item[1:]
            begin, end = trim(*span)
            selected = member_span(begin, end)
            if selected is not None:
                receiver, key, field = selected
                roots, _ = expression(receiver, anchor)
                fields, known = keys_for(native[slice(*key)], anchor, begin) if key is not None else ({field}, True)
                if not known:
                    fields.add(None)
                for root in roots:
                    for field in fields:
                        result.update(original_operands(("snapshot-member", ("value", root), field, begin, anchor)))
            watch_bucket(("raw-sources",))
            for key, sources_for_call in tuple(raw_sources.items()):
                selected = set().union(*(set(sources_for_call.get(value, ())) for value in values))
                if selected:
                    token(("raw-sources", key))
                    result.update(selected)
        # An opaque observer can replace an escaped member with another user
        # operand it retained. Preserve that provenance when an option has no
        # exact original, rather than certifying the remaining literal subset.
        if item[0] in {"span", "value", "snapshot-member"}:
            values, _ = value_of(item)
            origins = set()
            for value in values:
                watch_bucket(("affected", value))
                origins.update(affected.get(value, ()))
            if origins:
                watch_bucket(("escapes",))
                for events in tuple(escapes.values()):
                    result.update(operand for _, _, origin, operand in tuple(events)
                                  if origin in origins and operand is not None)
        return result - {item}

    def plain_const_binding(owner, name):
        events = variable_events.get((owner, name), ())
        if len(events) != 1:
            return False
        _, path, defaults, rest, _, at, context, certain = events[0]
        return (certain and context == -1 and not path and not defaults and rest is None
            and re.fullmatch(r"\s*const\s+", code[starts[at]:at]) is not None)

    def capability_origin(item, constraints, seen=frozenset()):
        # Certify original recipes, not an inexact singleton SCC result.
        if item in seen:
            return empty, False
        seen = seen | {item}
        if item[0] != "span":
            return empty, False
        begin, end = trim(*item[1])
        name = code[begin:end]
        identifier = re.fullmatch(r"[A-Za-z_$][\w$]*", name)
        owner = lexical(name, begin) if identifier else None
        spec = bindings_by_body.get(owner, {}).get(name)
        events = variable_events.get((owner, name), ()) if identifier else ()
        if spec is not None:
            if events or spec[1] or spec[2] or spec[3] is not None:
                return empty, False
            result, exact = set(), True
            for active in anchor_context(item[2], owner):
                if active is None or active[2] != owner:
                    return empty, False
                token(("anchor", active))
                edges = tuple(anchors[active])
                if active in constraints:
                    edges = tuple(edge for edge in edges if edge == constraints[active])
                if len(edges) > 1 and constraints and active not in constraints:
                    return empty, False
                for edge in edges:
                    selected, certain = packet_selection(edge[3], spec[0],
                        {**constraints, active: edge}, variants=True)
                    exact &= certain
                    for source, branch in selected:
                        values, known = capability_origin(source, branch, seen)
                        result.update(values)
                        exact &= known
            return frozenset(result), exact and bool(result) and len(result) == 1
        values, exact = value_of(item)
        if exact:
            return values, bool(values) and len(values) == 1 and all(
                root[0] in {"handle", "callable", "intrinsic"} for root in values)
        # A successful read of this const has its initializer's value;
        # a read in its temporal dead zone throws instead of yielding a callee.
        if not identifier or not plain_const_binding(owner, name):
            return empty, False
        defined, known = expression(events[0][4], None)
        if not known or len(defined) != 1 or next(iter(defined))[0] != "callable":
            return empty, False
        allowed = defined | {("binding", owner, name)}
        if not values or not values <= allowed:
            return empty, False
        return defined, True

    def effective_source_target(site, anchor, constraints, seen=frozenset()):
        targets, known, receiver = reference_call(call_sites[site][:2], anchor)
        if len(targets) != 1:
            # Immutable callable provenance is the only supported placeholder
            # exception; genuine competing roots never acquire a certificate.
            values, certain = capability_origin(recipe(call_sites[site][:2], anchor),
                constraints)
            return certain and len(values) == 1 and next(iter(values))[0] == "callable"
        target = next(iter(targets))
        if target in seen or target[0] in {"unknown", "binding"}:
            return False
        seen = seen | {target}
        if target[0] == "method":
            values, certain = capability_origin(receiver, constraints)
            return certain and len(values) == 1 and not effects.get((next(iter(values)), target[1]))
        if target[0] == "bound":
            token(("bind", target[1], target[2]))
            saved = bind_sites.get((target[1], target[2]), ())
            if not saved:
                return False
            for source, captured in saved:
                values, certain = capability_origin(source, constraints)
                if not certain or len(values) != 1 or source_layout(captured, constraints) is None:
                    return False
            return True
        if target in {("intrinsic", "Reflect.apply"), ("intrinsic", "Reflect.construct")}:
            actual = packet_nodes.get((site, anchor, "raw"))
            if actual is None:
                return False
            selected, exact = packet_selection(actual, 0, constraints, variants=True)
            if not exact:
                return False
            for source, branch in selected:
                values, certain = capability_origin(source, branch)
                if not certain or len(values) != 1:
                    return False
            return bool(selected)
        values, certain = capability_origin(recipe(call_sites[site][:2], anchor),
            constraints)
        return certain and values == targets

    def relay_sources(site, anchor, constraints, lineage, seen=frozenset()):
        current = site, anchor
        if current in seen or anchor is None or function_owner(site) != anchor[2]:
            return [(site, constraints)], False
        if not effective_source_target(site, anchor, constraints):
            return [(site, constraints)], False
        body = bodies_by_owner.get(anchor[2])
        if body is None or body[3] or body[4]:
            return [(site, constraints)], False
        begin, end, block = body[:3]
        if block:
            returns = return_sites.get(begin, ())
            if len(returns) != 1:
                return [(site, constraints)], False
            prefix = code[begin + 1:returns[0][0]].strip()
            suffix = code[returns[0][1]:end - 1].strip()
            if prefix != "return" or suffix not in {"", ";"}:
                return [(site, constraints)], False
            returned_span = trim(*returns[0])
        else:
            returned_span = trim(begin, end)
        if returned_span != trim(call_sites[site][0], call_sites[site][2] + 1):
            return [(site, constraints)], False
        edges = tuple(anchors.get(anchor, ()))
        if anchor in constraints:
            edges = tuple(edge for edge in edges if edge == constraints[anchor])
        if len(edges) > 1 and constraints and anchor not in constraints:
            return [(site, constraints)], False
        result = []
        for caller_site, caller, capture, _ in edges:
            if caller_site not in call_sites:
                # An accessor read has no following-application syntax.
                return [(site, constraints)], False
            state = states.get(("invoke", caller_site, caller))
            allowed = lineage | {("unknown", site, anchor), ("unknown", caller_site, caller)}
            if state is None or not (lineage & state[0]) or any(value not in allowed for value in state[0]):
                return [(site, constraints)], False
            branch = {**constraints, anchor: (caller_site, caller, capture, _)}
            endpoints, _ = relay_sources(caller_site, caller, branch, allowed, seen | {current})
            result.extend(endpoints)
        return result or [(site, constraints)], bool(result)

    def invocation(site, anchor):
        return query(("invoke", site, anchor), lambda: compute_invocation(site, anchor))

    def compute_invocation(site, anchor):
        # Replace this site's derived source facts on reevaluation. Published
        # possible effects remain monotone; exact source consumers use only
        # the settled invocation facts after the dependency queue is empty.
        sources[site, anchor] = set()
        admission = source_admissions[site, anchor] = set()
        if not native_live(anchor) or not defaults_selected(call_defaults.get(site, ()), anchor):
            return undefined
        begin, finish, end, tagged, parts = call_sites[site]
        if anchor is not None and isinstance(anchor[1], tuple):
            # A native array callback has a correlated original element and
            # index. Reuse expression selection for this actual invocation;
            # ordinary native body scheduling remains conservative.
            for region, condition, nullish, truth in gate_regions:
                if region[0] <= begin and end < region[1] and truth not in predicate(recipe(condition, anchor), nullish):
                    return undefined
        targets, known, this = reference_call((begin, finish), anchor)
        actual = operands(parts, anchor, site)
        outcomes = {}
        if not known:
            admission.add("other")
        values, exact = invoke_values(targets, this, actual, site, anchor, set(), outcomes, admission=admission, definite=known and len(targets) == 1)
        result = ("unknown", site, anchor)
        guaranteed = bool(outcomes.get(result)) and all(raw for raw, _ in outcomes[result])
        if raw_results.get(result, False) != guaranteed:
            raw_results[result] = guaranteed
            invalidate(("raw-result", result))
        originals = {value: frozenset(item for _, items in producers for item in items)
                     for value, producers in outcomes.items()}
        key = site, anchor
        if raw_sources.get(key) != originals:
            raw_sources[key] = originals
            invalidate(("raw-sources", key))
            invalidate(("raw-sources",))
        if not known and not values:
            values = frozenset({("unknown", site, anchor)})
        return values, exact and known

    def reached_values(item, site, anchor):
        return query(("reached-values", item, site, anchor), lambda: compute_reached_values(item, site, anchor))[0]

    def compute_reached_values(item, site, anchor):
        pending, seen, reached = [item], set(), {}
        while pending:
            selected = pending.pop()
            if selected in reached:
                continue
            values, _ = value_of(selected)
            reached[selected] = values
            for root in values:
                if root in seen:
                    continue
                seen.add(root)
                if root == ("global",):
                    pending.extend(("value", ("intrinsic", name)) for name in ("String", "Object", "Reflect"))
                if root[0] in {"aggregate", "global", "copy"}:
                    fields = set(aggregate_fields(root, site)[0]) if root[0] == "aggregate" else set()
                    if root[0] == "copy" or root[0] == "aggregate":
                        fields.add(None)
                    watch_bucket(("effects", root))
                    fields.update(field for owner, field in effects if owner == root)
                    for field in fields:
                        pending.append(("snapshot-member", ("value", root), field, site, anchor))
        return reached, False

    def observe_global(site, anchor):
        endpoint = call_sites[site][2] if site in call_sites else implicit_sites.get(site, (site, site))[1]
        grow(escapes, ("global",), ((endpoint, owners[site], (site, anchor), None),), ("escape", ("global",)))

    def opaque_call(this, actual, site, anchor, active, *, retain_receiver=True):
        # Opaque implementations may share retained user operands. Identity of
        # an unresolved receiver does not certify isolation from another one.
        inputs = list(actual)
        if retain_receiver:
            receivers, _ = value_of(this)
            if any(value[0] in {"aggregate", "global", "intrinsic", "copy"} for value in receivers):
                inputs.append(this)
        watch_bucket(("escapes",))
        for root, events in tuple(escapes.items()):
            for at, context, origin, operand in tuple(events):
                if operand is None or not native_live(origin[1]) or origin == (site, anchor) or not ordering(context, site, at)[0]:
                    continue
                for producer in tuple(relation_origins.get((("escape", root), (at, context, origin, operand)), ())):
                    if producer not in stack:
                        token(producer)
                inputs.append(("value", root) if root[0] in {"aggregate", "global", "copy"} else operand)
        # Observe only user publications. Ambient builtin eval is not an
        # escaped user capability, and known pre-observation overwrites apply.
        watch_bucket(("effects", ("global",)))
        inputs.extend(("snapshot-member", ("value", ("global",)), field, site, anchor)
                      for root, field in tuple(effects) if root == ("global",))
        for owner, name in variable_events:
            if owner is None and roots_global(name) is None:
                reference_value(name, site, anchor)
                key = name, site, anchor
                token(("reference-operands", key))
                inputs.extend(reference_operands.get(key, ()))
        reached = {}
        for item in inputs:
            reached.update(reached_values(item, site, anchor))
        origin = site, anchor
        for item, values in tuple(reached.items()):
            for value in values:
                if value[0] not in {"undefined", "data", "read-callable", "callable-data"}:
                    grow(escapes, value, ((site, owners[site], origin, item),), ("escape", value))
        handles = {value for values in reached.values() for value in values if value[0] == "handle"}
        operands = [item for item, values in reached.items()
                    if any(value[0] in {"string", "unknown", "binding"} for value in values)]
        for handle in handles:
            for item in operands:
                co_packet = packet((site, anchor, "cooperand", item), ("sequence", ((item, False),)))
                sources.setdefault((site, anchor), set()).add((handle[1], False, co_packet))
        for value in {value for values in reached.values() for value in values if value[0] in {"callable", "bound"}}:
            first_site, first_anchor = opaque_activations.setdefault(value, (site, anchor))
            arguments = packet((value, "opaque-arguments"), ("sequence", ((("value", ("unknown",)), True),)))
            invoke_values(frozenset({value}), ("value", ("unknown",)), arguments, first_site, first_anchor, active)

    def native_live(anchor, seen=frozenset()):
        if anchor is None or anchor in seen:
            return True
        token(("callback-guard", anchor))
        token(("anchor", anchor))
        edges = tuple(anchors.get(anchor, ()))
        guards = callback_guards.get(anchor, {})
        return not edges or any(native_live(edge[1], seen | {anchor}) and
            any(all(expected in predicate(item) for item, expected in condition)
                for condition in guards.get(edge, ((),))) for edge in edges)

    def native_callback(callback, callback_this, arguments, site, anchor, active, identity, admission, definite, guard):
        key = ("native-result", site, anchor, identity)
        edge = callback, callback_this, arguments, frozenset(active), definite, guard
        selected = callback_inputs.setdefault(key, set())
        if edge not in selected:
            selected.add(edge)
            invalidate(("native-inputs", key))
        def compute():
            token(("native-inputs", key))
            local, labels, values, exact = {}, set(), set(), True
            before = set(sources.get((site, anchor), ()))
            alternatives = tuple(callback_inputs.get(key, ()))
            for targets, receiver, actual, adapting, certified, condition in alternatives:
                if any(expected not in predicate(item) for item, expected in condition):
                    continue
                result, certain = invoke_values(targets, receiver, actual, site, anchor, adapting,
                    local, (), identity, labels, definite=certified and len(alternatives) == 1, guard=condition)
                values.update(result)
                exact &= certain
            items = {item for producers in local.values() for _, operands in producers for item in operands}
            represented = {value for value, producers in local.items() if any(operands for _, operands in producers)}
            items.update(("value", value) for value in values - represented)
            callback_items[key] = frozenset(items)
            callback_sources[key] = frozenset(sources.get((site, anchor), ())) - before
            callback_admissions[key] = frozenset(labels)
            return frozenset(values), exact
        values, exact = query(key, compute)
        sources.setdefault((site, anchor), set()).update(callback_sources.get(key, ()))
        if admission is not None:
            admission.update(callback_admissions.get(key, ()))
        item = ("choice", key, "return")
        selected = set(callback_items.get(key, ()))
        if choices.get(item) != selected:
            choices[item] = selected
            invalidate(("choices", item), producer=key)
        return item, values, exact

    def invoke_values(targets, this, actual, site, anchor, adapting, outcomes=None, actual_options=(), identity=None, admission=None, *, definite=False, guard=()):
        outcomes = {} if outcomes is None else outcomes
        identity = site if identity is None else identity
        returned, exact = set(), True
        endpoint = call_sites[site][2] if site in call_sites else implicit_sites.get(site, (site, site))[1]
        definite = definite and len(targets) == 1
        def return_operand(item):
            nonlocal exact
            values, known = value_of(item)
            returned.update(values)
            exact &= known
            for value in values:
                outcomes.setdefault(value, set()).add((False, (item,)))
        for root in targets:
            transition = root
            if transition in adapting:
                if admission is not None:
                    admission.add("other")
                exact = False
                continue
            active = adapting | {transition}
            tag = "string" if raw_result(root) else root[0]
            if admission is not None:
                if tag == "handle":
                    admission.add("source")
                elif tag not in {"method", "bound", "array-method", "read-callable", "callable-data", "data", "string", "undefined"} and root not in {("intrinsic", "Reflect.apply"), ("intrinsic", "Reflect.construct")}:
                    admission.add("other")
            if tag == "binding" and plain_const_binding(root[1], root[2]):
                continue
            if tag == "method":
                target_values, target_exact = value_of(this)
                if admission is not None and not target_exact:
                    admission.add("other")
                callable_values = frozenset(value for value in target_values if not raw_result(value) and value[0] in {"method", "handle", "callable", "bound", "intrinsic", "array-method", "read-callable", "unknown", "binding"})
                if not callable_values:
                    returned.add(("undefined",))
                    continue
                if root[1] == "bind":
                    selected = bind_sites.setdefault((site, anchor), set())
                    if (this, actual) not in selected:
                        selected.add((this, actual))
                        invalidate(("bind", site, anchor))
                    returned.add(("bound", site, anchor))
                    continue
                next_this = actual[0] if actual else ("value", ("undefined",))
                variants = tuple(array_operands(actual[1])) if root[1] == "apply" and len(actual) > 1 else (packet_view(actual, 1, site, anchor, active, "call"),)
                for selected in variants:
                    options = variants if root[1] == "apply" else tuple(packet_view(option, 1, site, anchor, active, "call") for option in actual_options)
                    values, certain = invoke_values(callable_values, next_this, selected, site, anchor, active, outcomes, options, identity, admission, definite=definite and target_exact and len(callable_values) == 1 and len(actual_options) <= 1, guard=guard)
                    returned.update(values)
                    exact &= certain and target_exact
            elif tag == "array-method":
                # Alternatives may change inserted arguments or callback selection.
                definite = definite and len(actual_options) <= 1
                receiver_values, receiver_exact = value_of(this)
                receiver_values = frozenset(value for value in receiver_values
                    if not (value[0] == "binding" and plain_const_binding(value[1], value[2])))
                method = root[1]
                for item in actual:
                    value_of(item)
                inserted = tuple(actual) if method in {"push", "unshift"} else ()
                begin = call_sites[site][0] if site in call_sites else site
                once = function_owner(site) == -1 and not any(phase(loop, site) is not None for loop in facts.get("loops", ()))
                certain = definite and receiver_exact and len(receiver_values) == 1 and once and independent_statement(begin, r"\s*(?:(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*)?(?:await\s*)?")
                def write_native(receiver, field, item, *, remove=False, length=None):
                    event = endpoint, owners[site], certain, item, anchor, None
                    if remove and certain:
                        array_removals[event] = True
                    if length is not None and certain:
                        array_lengths[event] = length
                    publish(receiver, field, item, endpoint, owners[site], certain, anchor)
                def collection(root, layouts):
                    layouts = tuple(layouts)
                    if copy_packets.get(root) != layouts:
                        copy_packets[root] = layouts
                        invalidate(("copy", root))
                    return root
                def finite_layout(receiver):
                    if receiver[0] != "copy" and not (receiver[0] == "aggregate" and code[receiver[1][0]] == "["):
                        return None
                    low, high = array_length(receiver, endpoint, anchor, len(code) + 1, {}, frozenset(), {})
                    if high is None or low != high:
                        return None
                    items = tuple(("snapshot-member", ("value", receiver), str(index), endpoint, anchor) for index in range(high))
                    if not all(value_of(item)[1] and all(kind == "values" and known for kind, _, known in property_options(receiver, str(index), endpoint, anchor, layout=True)) for index, item in enumerate(items)):
                        return None
                    return items
                closed = []
                if receiver_exact:
                    for receiver in receiver_values:
                        items = finite_layout(receiver)
                        if items is None:
                            break
                        closed.append((receiver, items))
                if len(closed) != len(receiver_values):
                    closed = []
                callback_methods = {"forEach", "every", "some", "filter", "find", "findIndex", "findLast", "findLastIndex", "map", "flatMap", "reduce", "reduceRight"}
                if method in callback_methods:
                    layouts = []
                    if actual:
                        callback, callback_exact = value_of(actual[0])
                        callback_this = actual[1] if method not in {"reduce", "reduceRight"} and len(actual) > 1 else ("value", ("undefined",))
                        for receiver in receiver_values:
                            finite = next((items for selected, items in closed if selected == receiver), None)
                            if method == "map" and finite is None:
                                finite = finite_layout(receiver)
                            contributions = sorted(array_contributions(receiver, endpoint, anchor,
                                holes=method in {"find", "findIndex", "findLast", "findLastIndex"}),
                                key=lambda entry: (entry[1] is None, entry[1] or 0, repr(entry[0])))
                            if method in {"findLast", "findLastIndex", "reduceRight"}:
                                contributions.reverse()
                            reducing = method in {"reduce", "reduceRight"}
                            accumulator = actual[1] if reducing and len(actual) > 1 else None
                            if reducing and accumulator is None and contributions:
                                accumulator = contributions.pop(0)[2]
                            guards, pieces = list(guard), []
                            if method == "map" and finite is not None:
                                pieces = [(recipe((site, site), anchor), False) for _ in finite]
                            for original, index, element in contributions:
                                if any(expected not in predicate(item) for item, expected in guards):
                                    break
                                value_of(element)
                                ordinal = ("index", index) if index is not None else ("value", ("data",))
                                source = ("value", receiver)
                                operands_ = ((accumulator, False), (element, False), (ordinal, False), (source, False)) if reducing else ((element, False), (ordinal, False), (source, False))
                                arguments = packet((site, anchor, "native-arguments", method, original, operands_), ("sequence", operands_))
                                selected_identity = (identity[0] if isinstance(identity, tuple) else site, original)
                                result, values, result_exact = native_callback(callback, callback_this, arguments, site, anchor, active,
                                    selected_identity, admission, definite and callback_exact, tuple(guards))
                                truth = predicate(result)
                                if reducing:
                                    choice = ("choice", (site, anchor, method, receiver, original), "accumulator")
                                    selected = {accumulator, result}
                                    if choices.get(choice) != selected:
                                        choices[choice] = selected
                                        invalidate(("choices", choice))
                                    accumulator = choice
                                elif method == "map":
                                    if finite is not None and index is not None:
                                        pieces[index] = result, False
                                    elif index is not None and index < len(code) + 1 and not any(spread for _, spread in pieces):
                                        while len(pieces) <= index:
                                            pieces.append((recipe((site, site), anchor), False))
                                        pieces[index] = result, False
                                    else:
                                        # An unknown input position remains an
                                        # unknown output position. Reuse opaque
                                        # spread and optional copy layouts; the
                                        # callback result remains one element,
                                        # including when that value is an array.
                                        optional = ("copy", site, (anchor, receiver, original, "map-position"))
                                        children = tuple(packet((optional, present), ("sequence", ((result, False),) if present else ())) for present in (False, True))
                                        collection(optional, children)
                                        pieces.extend(((("value", ("unknown",)), True), (("value", optional), True)))
                                elif method == "filter":
                                    if truth == frozenset({True}):
                                        pieces.append((element, False))
                                    elif True in truth:
                                        optional = ("copy", site, (anchor, receiver, original, "optional"))
                                        children = tuple(packet((optional, present), ("sequence", ((element, False),) if present else ())) for present in (False, True))
                                        collection(optional, children)
                                        pieces.append((("value", optional), True))
                                elif method == "flatMap":
                                    arrays = values and all(value[0] == "copy" or value[0] == "aggregate" and code[value[1][0]] == "[" for value in values)
                                    pieces.append((result, bool(arrays)))
                                elif method in {"find", "findLast"} and True in truth:
                                    return_operand(element)
                                if method in {"some", "every", "find", "findIndex", "findLast", "findLastIndex"}:
                                    guards.append((result, method == "every"))
                            if reducing:
                                if accumulator is not None:
                                    return_operand(accumulator)
                            elif method in {"map", "filter", "flatMap"}:
                                child = packet((site, anchor, method, receiver, tuple(pieces)), ("sequence", tuple(pieces)))
                                layouts.append(child)
                    if method in {"map", "filter", "flatMap"}:
                        returned.add(collection(("copy", site, anchor), layouts))
                    elif method in {"find", "findLast"}:
                        returned.add(("undefined",))
                    elif method not in {"reduce", "reduceRight"}:
                        returned.add(("undefined",) if method == "forEach" else ("data",))
                    exact &= receiver_exact
                    if admission is not None:
                        admission.add("other")
                elif closed and method in {"reverse", "pop", "shift", "push", "unshift", "sort", "at"} and not (method == "sort" and actual):
                    for receiver, items in closed:
                        size = len(items)
                        if method == "at":
                            names, known = operand_keys(actual[0]) if actual else ({"0"}, True)
                            if known and len(names) == 1 and all(numeric_field(name) for name in names):
                                index = int(next(iter(names)))
                                return_operand(items[index] if index < size else ("value", ("undefined",)))
                            else:
                                for item in items:
                                    return_operand(item)
                                returned.add(("undefined",))
                                exact = False
                        elif not certain:
                            # Multiple activations/targets have no single exact
                            # transform. Preserve item provenance without a
                            # foreign retention event or same-slot reprojection.
                            for item in (*items, *inserted):
                                write_native(receiver, None, item)
                            if method in {"pop", "shift"}:
                                for item in items:
                                    return_operand(item)
                                returned.add(("undefined",))
                            else:
                                returned.add(receiver if method in {"reverse", "sort"} else ("data",))
                        elif method in {"pop", "shift"}:
                            return_operand(items[-1 if method == "pop" else 0] if items else ("value", ("undefined",)))
                            if items:
                                if method == "shift":
                                    for index, item in enumerate(items[1:]):
                                        write_native(receiver, str(index), item)
                                write_native(receiver, str(size - 1), ("value", ("undefined",)), remove=True)
                                write_native(receiver, "length", ("index", size - 1), length=size - 1)
                        elif method in {"push", "unshift"}:
                            low, high = packet_length(actual, len(code) + 1)
                            if high is None or low != high:
                                for item in (*items, *inserted):
                                    write_native(receiver, None, item)
                                exact = False
                            else:
                                if method == "unshift":
                                    for index, item in enumerate(items):
                                        write_native(receiver, str(index + high), item)
                                for index in range(high):
                                    write_native(receiver, str(index + (size if method == "push" else 0)), actual[index])
                                write_native(receiver, "length", ("index", size + high), length=size + high)
                            returned.add(("data",))
                        else:
                            if method == "reverse":
                                for index, item in enumerate(reversed(items)):
                                    write_native(receiver, str(index), item)
                            else:
                                for item in items:
                                    write_native(receiver, None, item)
                                write_native(receiver, "length", ("index", size), length=size)
                            returned.add(receiver)
                    if admission is not None:
                        admission.add("other")
                elif method in {"reverse", "pop", "shift", "push", "unshift", "sort"} and not (method == "sort" and actual):
                    # Dispatch is known even when no exact receiver layout can
                    # be proved. Preserve possible native moves/insertions;
                    # lack of a layout certificate is not foreign execution.
                    certain = False
                    for receiver in receiver_values:
                        items = {item for _, _, item in array_contributions(receiver, endpoint, anchor)}
                        for item in (*items, *inserted):
                            write_native(receiver, None, item)
                        if method in {"pop", "shift"}:
                            for item in items:
                                return_operand(item)
                            returned.add(("undefined",))
                        else:
                            returned.add(receiver if method in {"reverse", "sort"} else ("data",))
                    exact = False
                    if admission is not None:
                        admission.add("other")
                elif method == "slice" and not actual and receiver_values and all(value[0] in {"aggregate", "copy"} for value in receiver_values):
                    for receiver in receiver_values:
                        for _, _, element in array_contributions(receiver, endpoint, anchor):
                            value_of(element)
                    copy_root = ("copy", site, anchor)
                    selected = copy_sources.setdefault(copy_root, set())
                    if this not in selected:
                        selected.add(this)
                        invalidate(("copy", copy_root))
                    returned.add(copy_root)
                    exact &= receiver_exact
                else:
                    if admission is not None:
                        admission.add("other")
                    opaque_call(this, actual, site, anchor, active)
                    returned.add(("unknown", site, anchor))
                    outcomes.setdefault(("unknown", site, anchor), set()).add((False, ()))
                    exact = False
            elif tag == "bound":
                token(("bind", root[1], root[2]))
                for target_recipe in {source for source, _ in tuple(bind_sites.get((root[1], root[2]), ()))}:
                    # Saved captures are the cross-descent boundary. This
                    # canonical node points back to finite bind edges instead
                    # of nesting previously derived packet identities.
                    capture = ("bind-capture", root[1], root[2])
                    captured = packet(capture, ("args", capture, 0))
                    target_values, target_exact = value_of(target_recipe)
                    if admission is not None and not target_exact:
                        admission.add("other")
                    bound_this = captured[0] if captured else ("value", ("undefined",))
                    prefix = packet_view(captured, 1, site, anchor, active, "bound-capture")
                    joined = packet_join(prefix, actual, site, anchor, active)
                    options = tuple(packet_join(prefix, option, site, anchor, active) for option in actual_options)
                    values, certain = invoke_values(target_values, bound_this, joined, site, anchor, active, outcomes, options, identity, admission, definite=definite and target_exact and len(target_values) == 1 and len(bind_sites.get((root[1], root[2]), ())) == 1, guard=guard)
                    returned.update(values)
                    exact &= certain and target_exact
            elif root == ("intrinsic", "Reflect.apply") or root == ("intrinsic", "Reflect.construct"):
                if actual:
                    target_values, target_exact = value_of(actual[0])
                    if admission is not None and not target_exact:
                        admission.add("other")
                    index = 1 if root[1].endswith("construct") else 2
                    reflect_this = actual[1] if index == 2 and len(actual) > 1 else ("value", ("undefined",))
                    variants = tuple(array_operands(actual[index])) if index < len(actual) else ((),)
                    for selected in variants:
                        values, certain = invoke_values(target_values, reflect_this, selected, site, anchor, active, outcomes, variants, identity, admission, definite=definite and target_exact and len(target_values) == 1 and len(actual_options) <= 1, guard=guard)
                        returned.update(values)
                        exact &= certain and target_exact
            elif tag == "handle":
                # Source conversion waits for settled packet and caller facts.
                if root[1] == "eval" and not actual:
                    returned.add(("undefined",))
                    continue
                if root[1] == "import" and actual:
                    values, certain = value_of(actual[0])
                    if certain and values and all(value[0] == "string" and not value[1].startswith(("data:", "blob:")) for value in values):
                        continue
                direct = site in call_sites and root[1] == "eval" and code[slice(*trim(call_sites[site][0], call_sites[site][1]))] == "eval" and "?." not in code[call_sites[site][1]:site]
                first_item = actual[0] if root[1] == "eval" and actual else None
                converted = packet((site, anchor, "eval-first", actual.key if isinstance(actual, Packet) else actual, first_item), ("sequence", ((first_item, False),))) if first_item is not None else actual
                if root[1] == "eval" and actual:
                    first, first_exact = value_of(first_item)
                    if first_exact and first and all(value[0] not in {"string", "unknown", "binding"} for value in first):
                        returned.update(first)
                        continue
                if root[1] == "constructor" and site in call_sites and call_sites[site][3]:
                    template_span = site, call_sites[site][2] + 1
                    # The first tag operand is an array. Its single cooked
                    # element is the body only without substitutions; otherwise
                    # it is a parameter conversion, followed by the actual
                    # substitution arguments with the last supplying the body.
                    # Unresolved array coercion stays an opaque parameter span.
                    pieces = tuple((recipe(template_span, anchor) if index == 0 else actual[index], False)
                                   for index in range(len(call_sites[site][4])))
                    converted = packet((site, anchor, "template-arguments"), ("sequence", pieces))
                sources.setdefault((site, anchor), set()).add((root[1], direct, converted))
                returned.add(("unknown", site, anchor))
                outcomes.setdefault(("unknown", site, anchor), set()).add((False, ()))
                exact = False
            elif root == ("intrinsic", "String.raw"):
                value = ("unknown", site, anchor)
                original = tuple(actual)
                if site in call_sites and call_sites[site][3]:
                    end = call_sites[site][2] + 1
                    original = (recipe((call_sites[site][0], end), anchor), *tuple(actual)[1:])
                    try:
                        value = ("string", javascript_string("String.raw" + native[site:end]))
                    except ValueError:
                        pass
                returned.add(value)
                outcomes.setdefault(value, set()).add((True, original))
            elif root == ("intrinsic", "Reflect.get"):
                if len(actual) < 2:
                    returned.add(("unknown", site, anchor))
                    exact = False
                    continue
                receivers, receiver_exact = value_of(actual[0])
                if actual[1][0] == "span":
                    names, key_exact = keys_for(native[slice(*actual[1][1])], actual[1][2], site)
                else:
                    names, key_exact = operand_keys(actual[1])
                if not key_exact:
                    names.add(None)
                exact &= receiver_exact and key_exact
                for field in names:
                    values, certain = project(receivers, receiver_exact, field, endpoint, anchor)
                    returned.update(values)
                    exact &= certain
            elif tag == "intrinsic" and root[1] in {"__defineGetter__", "__defineSetter__"}:
                receivers, known = value_of(this)
                if len(actual) >= 2:
                    names, key_exact = operand_keys(actual[0])
                    if not key_exact:
                        names.add(None)
                    item = ("descriptor", actual[1], endpoint, anchor)
                    methods = frozenset({actual[1]})
                    direct_accessors[item] = (methods, None) if root[1] == "__defineGetter__" else (None, methods)
                    begin = call_sites[site][0] if site in call_sites else site
                    certain = definite and len(actual_options) <= 1 and known and key_exact and len(receivers) == len(names) == 1 and independent_statement(begin, r"\s*")
                    for receiver in receivers:
                        for field in names:
                            publish(receiver, field, item, endpoint, owners[site], certain, anchor)
                returned.add(("undefined",))
            elif tag == "intrinsic" and root[1] in {"Object.assign", "Object.defineProperty", "Object.defineProperties", "Reflect.set", "Reflect.defineProperty"}:
                if not actual:
                    continue
                target_values, certain = value_of(actual[0])
                exact &= certain
                returned.update(target_values)
                context, at = owners[site], endpoint
                begin = call_sites[site][0] if site in call_sites else site
                definite = definite and certain and len(target_values) == 1 and independent_statement(begin, r"\s*(?:(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*)?(?:await\s*)?")
                def covers(option, index, field, *, key=False):
                    low, _ = packet_length(option, len(code) + 1)
                    if low <= index or not option:
                        return False
                    targets, exact = value_of(option[0])
                    if not exact or targets != target_values:
                        return False
                    values, exact = value_of(option[index])
                    if key:
                        if option[index][0] == "span":
                            names, exact = keys_for(native[slice(*option[index][1])], option[index][2], site)
                            return exact and names == {field}
                        return exact and values == frozenset({("string", field)})
                    if not exact or not values:
                        return False
                    for value in values:
                        fields, known = enumeration_fields(value, endpoint, anchor) if value[0] == "aggregate" else (set(), False)
                        if not known or field not in fields or not copy_presence(value, field, endpoint, anchor)[1]:
                            return False
                    return True
                low, high = packet_length(actual, len(code) + 1)
                if root[1] == "Object.assign":
                    donors = tuple((actual[index], definite and index < low, index) for index in range(1, high)) if high is not None else tuple((item, False, None) for item in packet_view(actual, 1, site, anchor, active, "donors"))
                    for donor, present, index in donors:
                        values, certain = value_of(donor)
                        exact &= certain
                        layouts = [enumeration_fields(value, endpoint, anchor) if value[0] == "aggregate" else (set(), value[0] in {"undefined", "data", "string"}) for value in values]
                        fields = set().union(*(set(fields) for fields, _ in layouts))
                        if not certain or any(not known for _, known in layouts):
                            fields.add(None)
                        for field in fields:
                            covered = present and certain and bool(layouts) and all(known and field in fields for fields, known in layouts) and all(copy_presence(value, field, endpoint, anchor)[1] for value in values) and all(covers(option, index, field) for option in actual_options)
                            item = ("snapshot-member", donor, field, endpoint, anchor)
                            value_of(item)
                            for target in target_values:
                                set_property(target, field, item, at, context, covered, anchor)
                else:
                    descriptors = []
                    if root[1] == "Object.defineProperties" and len(actual) >= 2:
                        values, known = value_of(actual[1])
                        layouts = [enumeration_fields(value, endpoint, anchor) if value[0] == "aggregate" else (set(), False) for value in values]
                        fields = set().union(*(set(fields) for fields, _ in layouts))
                        if not known or any(not certain for _, certain in layouts):
                            fields.add(None)
                        for field in fields:
                            covered = definite and low > 1 and known and bool(layouts) and all(certain and field in fields for fields, certain in layouts) and all(copy_presence(value, field, endpoint, anchor)[1] for value in values) and all(covers(option, 1, field) for option in actual_options)
                            descriptors.append((field, ("snapshot-member", actual[1], field, endpoint, anchor), covered))
                    elif len(actual) >= 3:
                        key_values, known = value_of(actual[1])
                        names = {value[1] for value in key_values if value[0] == "string"}
                        # The existing key owner also handles numeric literals.
                        if actual[1][0] == "span":
                            numeric, certain = keys_for(native[slice(*actual[1][1])], actual[1][2], site)
                            names, known = names | numeric, certain
                        else:
                            known &= len(names) == len(key_values)
                        if not names or not known:
                            names.add(None)
                        descriptors.extend((field, actual[2], definite and low > 2 and known and len(names) == 1 and field is not None and all(covers(option, 1, field, key=True) and packet_length(option, len(code) + 1)[0] > 2 for option in actual_options)) for field in names)
                    for field, descriptor, covered in descriptors:
                        item = descriptor if root[1] == "Reflect.set" else ("descriptor", descriptor, endpoint, anchor)
                        for target in target_values:
                            if root[1] == "Reflect.set":
                                set_property(target, field, item, at, context, covered, anchor)
                            else:
                                publish(target, field, item, at, context, covered, anchor)
            elif tag == "callable":
                body = callable_body(root[1])
                if body is None:
                    exact = False
                    continue
                begin_body, end_body, block, asynchronous, generator, parameters = body
                # Materialize formal recipes through this same expression owner.
                expression(root[1], root[2])
                origin = anchor[0] if anchor is not None else site
                invoked = origin, identity, begin_body
                edge = site, anchor, root[2], actual
                if isinstance(identity, tuple):
                    selected_guards = callback_guards.setdefault(invoked, {}).setdefault(edge, set())
                    if guard not in selected_guards:
                        selected_guards.add(guard)
                        invalidate(("callback-guard", invoked))
                selected = anchors.setdefault(invoked, set())
                grow(receiver_edges, invoked, ((edge, this, root[1] in callable_arrows),), ("receivers", invoked))
                if edge not in selected:
                    selected.add(edge)
                    invalidate(("anchor", invoked))
                    for index, path, defaults, rest in bindings_by_body.get(begin_body, {}).values():
                        if path or defaults:
                            operand = actual[index] if isinstance(actual, Packet) else ("value", ("undefined",))
                            value_of(select_path(operand, path, defaults, rest, begin_body, invoked))
                    for nested in body_sites.get(begin_body, ()):
                        schedule(nested, invoked)
                    for event in body_effects.get(begin_body, ()):
                        qualify_target(event, invoked)
                    for read in body_reads.get(begin_body, ()):
                        evaluate_read(read, invoked)
                if generator or asynchronous and not is_awaited(site):
                    returned.add(("data",))
                    continue
                returns = return_sites.get(begin_body, ()) if block else ((begin_body, end_body),)
                if not returns:
                    returned.add(("undefined",))
                for span in returns:
                    values, certain = expression(span, invoked)
                    returned.update(values)
                    exact &= certain
                    for value in values:
                        outcomes.setdefault(value, set()).add((False, (recipe(span, invoked),)))
            elif tag in {"read-callable", "callable-data", "data", "string", "undefined"}:
                returned.add(("undefined",))
            else:
                if tag in {"unknown", "binding"}:
                    observe_global(site, anchor)
                opaque_call(this, actual, site, anchor, active)
                origins = affected_at(root, site, anchor)
                result = ("unknown", site, anchor)
                if origins:
                    grow(affected, result, origins, ("affected", result))
                    capabilities = frozenset({("handle", "eval"), ("handle", "constructor"), ("handle", "import")})
                    invoke_values(capabilities, this, actual, site, anchor, active, outcomes, (), identity, admission)
                    returned.update(capabilities)
                returned.add(result)
                outcomes.setdefault(result, set()).add((False, ()))
                exact = False
        return frozenset(returned), exact

    def is_awaited(site):
        if site not in call_sites:
            return False
        begin = call_sites[site][0]
        before = preceding(begin)
        word_begin = before
        while word_begin and (code[word_begin - 1].isalnum() or code[word_begin - 1] in "_$"):
            word_begin -= 1
        return code[word_begin:before] == "await"

    def qualify_target(event, anchor):
        return query(("target", event, anchor), lambda: compute_target(event, anchor))

    def compute_target(event, anchor):
        if not native_live(anchor) or not defaults_selected(target_defaults.get(event, ()), anchor):
            return empty, True
        target, path, defaults, rest, rhs, at, context, certain = event
        begin, end = trim(*target)
        selected = member_span(begin, end)
        if selected is None:
            name = code[begin:end]
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", name) or lexical(name, begin) is not None:
                return empty, True
            names, keys_exact = {name}, True
            roots, exact = frozenset({("global",)}), True
        else:
            receiver_span, key_span, name = selected
            names, keys_exact = keys_for(native[slice(*key_span)], anchor, at) if key_span is not None else ({name}, True)
            roots, exact = expression(receiver_span, anchor)
        if not keys_exact:
            names.add(None)
        item = select_path(recipe(rhs, anchor), path, defaults, rest, rhs[1], anchor)
        producer = ("target", event, anchor)
        qualified = roots, exact, certain and keys_exact and len(names) == 1
        if qualifications.get(producer) != qualified:
            qualifications[producer] = qualified
            invalidate(("qualification", producer))
        for root in roots:
            for field in names:
                set_property(root, field, item, rhs[1] - 1, context, certain, anchor, producer)
        return empty, exact

    ending_sites = {}
    for site, (_, _, end, _, _) in call_sites.items():
        ending_sites.setdefault(end + 1, []).append(site)
    return_sites = {}
    for match in re.finditer(r"(?<![\w.$])return\b", code):
        owner = function_owner(match.start())
        end = node_expression_parts(code, match.end(), len(code), ";\n", first=True)[0][1]
        return_sites.setdefault(owner, []).append((match.end(), end))
    read_gates, read_defaults, call_defaults, target_defaults, gate_regions, possible_bodies = {}, {}, {}, {}, [], set()
    roots = set(facts.get("expression_spans", ()))
    for _, _, _, _, parts in call_sites.values():
        for span in parts:
            if span is not None:
                roots.add(span)
                body = callable_body(span)
                if body is not None:
                    possible_bodies.add(body[0])
                    if not body[2]:
                        roots.add((body[0], body[1]))
    for events in variable_events.values():
        roots.update(event[4] for event in events)
    roots.update(event[4] for event in effect_sites)
    roots.update(span for spans in return_sites.values() for span in spans)
    targets = {trim(*event[0]) for event in effect_sites}
    candidates = set()
    for match in re.finditer(r"(?<!\.)\.\s*[A-Za-z_$][\w$]*", code):
        candidates.add(callee(match.end()))
    for opening, closing in pairs.items():
        if opening < closing and code[opening:opening + 1] == "[":
            candidates.add(callee(closing + 1))
    for span in candidates:
        begin, end = trim(*span)
        if begin >= end or member_span(begin, end) is None or (begin, end) in targets:
            continue
        if re.search(r"\bdelete\s*$", code[starts[begin]:begin]):
            read_sites[(span, "delete")] = function_owner(begin)
            continue
        read_sites[(span, "get")] = function_owner(begin)
        finish = node_expression_parts(code, starts[begin], len(code), ";\n", first=True)[0][1]
        roots.add((starts[begin], finish))
    for match in re.finditer(r"\.\.\.", code):
        finish = node_expression_parts(code, match.end(), len(code), ",;\n", first=True)[0][1]
        span = match.end(), finish
        read_sites[(span, "copy")] = function_owner(match.start())
    for events in variable_events.values():
        for event in events:
            if event[1] or event[2] or event[3] is not None:
                read_sites[(event[4], event)] = function_owner(event[5])
    pending_roles, visited_roles = list(roots), set()
    while pending_roles:
        span = pending_roles.pop()
        normalized = trim(*span)
        if normalized in visited_roles or callable_body(normalized) is not None:
            continue
        visited_roles.add(normalized)
        parts = role(span)
        if parts is None:
            continue
        op, left, *arms = parts
        pending_roles.extend((left, *arms))
        if op == "?:":
            gate_regions.extend((arm, left, False, truth) for arm, truth in zip(arms, (True, False)))
        elif op != ",":
            gate_regions.append((arms[0], left, op == "??", op in {"&&", "??"}))
    for read, owner in read_sites.items():
        span = read[0]
        read_gates[read] = tuple((condition, nullish, truth) for region, condition, nullish, truth in gate_regions
                                 if region[0] <= span[0] and span[1] <= region[1])
        body_reads.setdefault(owner, []).append(read)

    def evaluate_read(read, anchor):
        return query(("evaluated-read", read, anchor), lambda: compute_read(read, anchor))

    def compute_read(read, anchor):
        if not native_live(anchor):
            return empty, True
        span, kind = read
        for condition, nullish, truth in read_gates[read]:
            if truth not in predicate(recipe(condition, anchor), nullish):
                return empty, True
        for owner, index, path in read_defaults.get(read, ()):
            selected_default = False
            for active in anchor_context(anchor, owner):
                if active is None or active[2] != owner:
                    selected_default = True
                    continue
                token(("anchor", active))
                for _, _, _, actual in tuple(anchors[active]):
                    item = actual[index] if isinstance(actual, Packet) else ("value", ("undefined",))
                    values, exact = value_of(select_path(item, path, (), None, span[0], active))
                    selected_default |= not exact or not values or any(value[0] in {"undefined", "unknown", "binding"} for value in values)
            if not selected_default:
                return empty, True
        if kind == "get":
            expression(span, anchor)
        elif kind == "delete":
            begin, end = trim(*span)
            receiver, key, field = member_span(begin, end)
            roots, known = expression(receiver, anchor)
            names, exact = keys_for(native[slice(*key)], anchor, begin) if key is not None else ({field}, True)
            if not exact:
                names.add(None)
            for root in roots:
                for name in names:
                    previous = [event for event in tuple(effects.get((root, name), ())) if ordering(event[1], begin, event[0])[0] and event[0] < begin]
                    ordinary = bool(previous) and all(event[3][0] != "descriptor" for event in previous)
                    certain = root == ("global",) and known and exact and len(roots) == len(names) == 1 and ordinary and independent_statement(begin, r"\s*delete\s*")
                    publish(root, name, ("value", ("undefined",)), end - 1, owners[begin], certain, anchor, ("evaluated-read", read, anchor))
        elif kind == "copy":
            values, _ = expression(span, anchor)
            for root in values:
                if root[0] == "aggregate":
                    fields, _ = enumeration_fields(root, span[0], anchor)
                    for field in fields:
                        project(frozenset({root}), True, field, span[0], anchor)
                    project(frozenset({root}), True, None, span[0], anchor)
        else:
            _, path, defaults, rest, rhs, _, _, _ = kind
            value_of(select_path(recipe(rhs, anchor), path, defaults, rest, rhs[1], anchor))
        return empty, True

    def defaults_selected(specs, anchor):
        for owner, index, path in specs:
            selected = False
            for active in anchor_context(anchor, owner):
                if active is None or active[2] != owner:
                    continue
                token(("anchor", active))
                for _, _, _, actual in tuple(anchors[active]):
                    item = actual[index] if isinstance(actual, Packet) else ("value", ("undefined",))
                    values, exact = value_of(select_path(item, path, (), None, owner, active))
                    selected |= not exact or not values or any(value[0] in {"undefined", "unknown", "binding"} for value in values)
            if not selected:
                return False
        return True
    # Force declaration/parameter descriptors before any caller query. This is
    # syntax only; expression results remain demanded and anchor-specific.
    for spans in declarations.values():
        for span in spans:
            body = callable_body(span)
            if body is not None:
                specs = bindings_by_body.setdefault(body[0], {})
                for index, parameter in enumerate(body[-1]):
                    targets = []
                    list(javascript_binding_names(code, *parameter, _facts=targets, _native=native))
                    for target, path, defaults, rest in targets:
                        name = code[slice(*target)].strip()
                        if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                            specs[name] = index, path, defaults, rest
    # Parameter defaults occupy the enclosing syntax scope but evaluate only
    # on the selected callee edge, not when the callable is merely defined.
    for body in tuple(body_cache.values()):
        if body is None:
            continue
        for index, parameter in enumerate(body[-1]):
            targets = []
            list(javascript_binding_names(code, *parameter, _facts=targets, _native=native))
            for _, path, defaults, _ in targets:
                for depth, default in defaults:
                    for site, call in call_sites.items():
                        if default[0] <= call[0] and call[2] < default[1]:
                            call_defaults.setdefault(site, set()).add((body[0], index, path[:depth]))
                    for event in effect_sites:
                        if default[0] <= event[5] and event[4][1] <= default[1]:
                            target_defaults.setdefault(event, set()).add((body[0], index, path[:depth]))
                    for read, owner in tuple(read_sites.items()):
                        span = read[0]
                        if default[0] <= span[0] and span[1] <= default[1] and owner == function_owner(parameter[0]):
                            body_reads[owner].remove(read)
                            read_sites[read] = body[0]
                            body_reads.setdefault(body[0], []).append(read)
                            read_defaults.setdefault(read, set()).add((body[0], index, path[:depth]))
    for event in effect_sites:
        qualify_target(event, None)
    for site in call_sites:
        schedule(site, None)
    for read, owner in read_sites.items():
        if owner == -1 or owner in possible_bodies:
            evaluate_read(read, None)
    task_cursor = 0
    while True:
        while task_cursor < len(tasks):
            task = tasks[task_cursor]
            task_cursor += 1
            queued.discard(task)
            if task[0] == "query":
                key = task[1]
                query(key, computations[key])
            elif task[0] == "target":
                qualify_target(task[1], task[2])
            else:
                invocation(task[1], task[2])
        bodies_by_owner = {body[0]: body for body in body_cache.values() if body is not None}
        emitted, buffered = set(), []
        source_snapshot = tuple((key, frozenset(values)) for key, values in sources.items())
        source_facts = frozenset(source_snapshot)
        state_count = len(states)
        for (site, anchor), selected in source_snapshot:
            if not native_live(anchor):
                continue
            end = call_sites[site][2] if site in call_sites else implicit_sites[site][1]
            for kind, direct, actual in selected:
                layout = source_layout(actual, {})
                variants, exact = source_variants(actual) if layout is not None else ([], False)
                if not exact:
                    # An original parenthesized argument span is unresolved by
                    # javascript_literal. It prevents compressed formal binding.
                    prefix = (site, end + 1)
                    variants = [((prefix, body) if kind == "constructor" else (body,), {})
                        for body in opaque_sources(actual)]
                for parts, constraints in variants:
                    endpoints = [(site, constraints)]
                    if kind == "constructor" and exact:
                        lineage = {("unknown", site, anchor)}
                        endpoints, _ = relay_sources(site, anchor, constraints, lineage)
                    for effective, _ in endpoints:
                        record = effective + 1, call_sites[effective][2] if effective in call_sites else implicit_sites[effective][1], kind, direct, tuple(parts)
                        if record not in emitted:
                            emitted.add(record)
                            buffered.append(record)

        # Conversion may demand new getters, recipes or source facts.
        # Rebuild from settled facts before exposing any public record.
        if (task_cursor < len(tasks) or len(states) != state_count
                or frozenset((key, frozenset(values)) for key, values in sources.items()) != source_facts):
            continue
        admissions_by_site = {}
        for (site, _), observed in source_admissions.items():
            admissions_by_site.setdefault(site, set()).update(observed)
        facts["source_only_regions"] = tuple((site + 1, call_sites[site][2])
            for site, observed in admissions_by_site.items()
            if site in call_sites and "source" in observed and "other" not in observed)
        for record in buffered:
            yield *record[:4], list(record[4])
        break


def javascript_source_calls(native, code, *, global_this=False, literal_atoms=(), templates=None, _facts=None):
    """Select invoked source handles from the shared lexical recipe graph."""
    facts = {}
    javascript_bindings(code, _facts=facts, _direct_eval_sites=())
    for begin, end, kind, direct, parts in _javascript_recipe_graph(native, code, facts,
            global_this=global_this, literal_atoms=literal_atoms, templates=templates):
        yield begin, end, kind, direct, parts
    if _facts is not None:
        _facts["source_only_regions"] = facts.get("source_only_regions", ())


def javascript_binding_names(code, start, end, *, _facts=None, _native=None):
    """Share binding names and lexical target projections with RHS discovery."""
    native = code if _native is None else _native

    def targets(start, end, path=(), defaults=(), rest=None):
        while start < end and code[start].isspace():
            start += 1
        while end > start and code[end - 1].isspace():
            end -= 1
        if code[start:start + 3] == "...":
            yield from targets(start + 3, end, path, defaults, (path[:-1], path[-1]) if path else ((), None))
            return
        selected = node_expression_parts(code, start, end, "=", first=True)[0]
        if selected[1] < end:
            defaults = (*defaults, (len(path), (selected[1] + 1, end)))
            end = selected[1]
            while end > start and code[end - 1].isspace():
                end -= 1
        opening = code[start:start + 1]
        if opening in {"{", "["}:
            for index, (begin, finish) in enumerate(node_expression_parts(code, start + 1, end - 1)):
                if not native[begin:finish].strip():
                    continue
                target_begin = begin
                while target_begin < finish and code[target_begin].isspace():
                    target_begin += 1
                if code[target_begin:target_begin + 3] == "...":
                    marker = (path, str(index) if opening == "[" else "object")
                    yield from targets(target_begin + 3, finish, path, defaults, marker)
                    continue
                parts = node_expression_parts(code, begin, finish, ":") if opening == "{" else []
                if opening == "[":
                    field = str(index)
                else:
                    field = native[slice(*parts[0])].strip()
                    if len(parts) == 1:
                        field = field.split("=", 1)[0].strip()
                    if not re.fullmatch(r"[A-Za-z_$][\w$]*|\d+", field):
                        if field.startswith("[") and field.endswith("]"):
                            key_start = parts[0][0]
                            while key_start < parts[0][1] and native[key_start].isspace():
                                key_start += 1
                            field = ("key", (key_start + 1, parts[0][1] - 1))
                        else:
                            try:
                                field = javascript_string(field)
                            except ValueError:
                                field = None
                target = (parts[0][1] + 1, finish) if len(parts) > 1 else (begin, finish)
                yield from targets(*target, (*path, field), defaults, rest)
            return
        if _facts is not None:
            _facts.append(((start, end), path, defaults, rest))
        name = code[start:end]
        if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
            yield name, start

    yield from targets(start, end)


def javascript_bindings(code, *, _direct_eval_sites, sloppy=False, _facts=None):
    """Keep single-site literal proof and expose its indexed binding facts.

    Command proof stays conservative across scopes. Source-handle discovery
    requests the same sites with lexical containment, without recursing through
    the direct-eval invalidation that consumes source-handle discovery itself.
    Native-aware callers supply their exact source's direct-eval sites because
    projected literals alone cannot distinguish strings from empty operands.
    """
    sites, declarations, mutable = {}, {}, set()
    target_spans, expression_spans, pattern_spans = set(), set(), set()
    records, ranges, functions, pairs, stack, callable_declarations = [], {}, set(), {}, [], []
    parameter_scopes = []
    if _facts is not None:
        for position, char in enumerate(code):
            if char in "([{":
                stack.append(position)
            elif char in ")]}":
                if stack:
                    begin = stack.pop()
                    pairs[begin], pairs[position] = position, begin
                    if code[begin] == "{":
                        ranges[begin] = position + 1

    def bind(start, end, owner=None, kind=None):
        if _facts is not None:
            begin = start
            while begin < end and code[begin].isspace():
                begin += 1
            if code[begin:begin + 1] in {"{", "["}:
                pattern_spans.add((begin, end))
        for name, position in javascript_binding_names(code, start, end):
            sites.setdefault(name, set()).add(position)
            if _facts is not None and kind is not None:
                records.append((name, position, owner, kind))

    def parameters(opening, owner=None):
        for begin, end in node_expression_parts(code, opening + 1, len(code)):
            bind(begin, end, owner, "parameter")
            if _facts is not None and owner is not None:
                parameter_scopes.append((begin, end, owner))

    def body(opening):
        if _facts is None:
            return None
        closing = pairs.get(opening)
        if closing is None:
            return None
        begin = closing + 1
        while begin < len(code) and code[begin].isspace():
            begin += 1
        return begin if code[begin:begin + 1] == "{" else None

    for declaration in re.finditer(r"\b(const|let|var)\s+", code):
        end = node_expression_parts(code, declaration.end(), len(code), ";\n", first=True)[0][1]
        for begin, finish in node_expression_parts(code, declaration.end(), end):
            assignment = node_expression_parts(code, begin, finish, "=")
            target_end = assignment[0][1]
            loop = re.search(r"\b(?:of|in)\b", code[begin:target_end])
            if loop:
                target_end = begin + loop.start()
            if _facts is not None:
                target_begin, target_finish = begin, target_end
                while target_begin < target_finish and code[target_begin].isspace():
                    target_begin += 1
                while target_finish > target_begin and code[target_finish - 1].isspace():
                    target_finish -= 1
                target_spans.add((target_begin, target_finish))
                if len(assignment) > 1:
                    expression_spans.add((target_end + 1, finish))
            bind(begin, target_end, ("at", declaration.start()) if _facts is not None else None, declaration[1])
            named = re.fullmatch(r"\s*([A-Za-z_$][\w$]*)\s*", code[begin:target_end])
            if named and len(assignment) > 1:
                declarations[named[1]] = (target_end + 1, finish)
                if declaration[1] != "const":
                    mutable.add(named[1])
    for function in re.finditer(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)?\s*\(", code):
        owner = body(function.end() - 1)
        if owner is not None:
            functions.add(owner)
        if function[1]:
            bind(function.start(1), function.end(1), kind="function")
            if owner in pairs:
                begin = function.start()
                before = begin
                while before and code[before - 1].isspace():
                    before -= 1
                if code[max(0, before - 5):before] == "async" and (before == 5 or not re.match(r"[\w$]", code[before - 6])):
                    begin = before - 5
                    before = begin
                    while before and code[before - 1].isspace():
                        before -= 1
                if not before or code[before - 1] in ";{}" or "\n" in code[before:begin] and code[before - 1] in ")]":
                    callable_declarations.append((function[1], function.start(1), (begin, pairs[owner] + 1)))
        parameters(function.end() - 1, owner)
    for arrow in re.finditer(r"=>", code):
        owner = None
        if _facts is not None:
            owner = arrow.end()
            while owner < len(code) and code[owner].isspace():
                owner += 1
            if code[owner:owner + 1] != "{":
                ranges[owner] = node_expression_parts(code, owner, len(code), ",;\n", first=True)[0][1]
            functions.add(owner)
        end = arrow.start()
        while end and code[end - 1].isspace():
            end -= 1
        if end and code[end - 1] == ")":
            depth, opening = 1, end - 2
            while opening >= 0 and depth:
                depth += (code[opening] == ")") - (code[opening] == "(")
                opening -= 1
            if not depth:
                parameters(opening + 1, owner)
        else:
            begin = end
            while begin and (code[begin - 1].isalnum() or code[begin - 1] in "_$"):
                begin -= 1
            name = re.fullmatch(r"[A-Za-z_$][\w$]*", code[begin:end])
            if name:
                bind(begin, end, owner, "parameter")
    for caught in re.finditer(r"\bcatch\s*\(", code):
        parameters(caught.end() - 1, body(caught.end() - 1))
    for loop in re.finditer(r"\bfor\s*(?:await\s*)?\(\s*(?!const\b|let\b|var\b)", code):
        end = node_expression_parts(code, loop.end(), len(code), ";", first=True)[0][1]
        delimiter = re.search(r"\b(?:of|in)\b", code[loop.end():end])
        if delimiter:
            bind(loop.end(), loop.end() + delimiter.start())
    for assignment in re.finditer(r"[\]}]\s*=(?!=|>)", code):
        finish = assignment.start() + 1
        depth, opening = 1, assignment.start() - 1
        while opening >= 0 and depth:
            depth += (code[opening] in "]}") - (code[opening] in "[{")
            opening -= 1
        if not depth and (opening < 0 or not re.match(r"[\w$)\]]", code[opening])):
            bind(opening + 1, finish)
    if _facts is None and _direct_eval_sites:
        for name in sites if sloppy else mutable:
            sites[name].add(len(code))
    member_containers, headers = set(), set()
    brace_owner, brace_stack = {}, []
    if _facts is not None:
        for position, char in enumerate(code):
            while brace_stack and pairs[brace_stack[-1]] <= position:
                brace_stack.pop()
            brace_owner[position] = brace_stack[-1] if brace_stack else None
            if char == "{" and position in pairs:
                brace_stack.append(position)
        # These are expression roots established by the shared syntax scanner:
        # declaration/assignment RHSs, return values, and paired operands.
        # A label's statement block is none of those roots. Function bodies are
        # barriers because paired traversal never descends arbitrary braces.
        for assignment in re.finditer(r"(?<![=!<>])=(?!=|>)", code):
            finish = node_expression_parts(code, assignment.end(), len(code), ",;\n", first=True)[0][1]
            expression_spans.add((assignment.end(), finish))
        for returned in re.finditer(r"(?<![\w.$])return\b", code):
            finish = node_expression_parts(code, returned.end(), len(code), ";\n", first=True)[0][1]
            expression_spans.add((returned.end(), finish))
        for opening in pairs:
            if code[opening:opening + 1] == "(" and pairs[opening] > opening:
                expression_spans.update(node_expression_parts(code, opening + 1, pairs[opening]))
        pending, seen = list(expression_spans), set()
        while pending:
            begin, end = pending.pop()
            while True:
                while begin < end and code[begin].isspace():
                    begin += 1
                while end > begin and code[end - 1].isspace():
                    end -= 1
                if code[begin:begin + 1] == "(" and pairs.get(begin) == end - 1:
                    begin, end = begin + 1, end - 1
                else:
                    break
            if begin >= end or (begin, end) in seen:
                continue
            seen.add((begin, end))
            if code[begin:begin + 1] in {"{", "["} and begin in pairs and pairs[begin] < end:
                close = pairs[begin]
                if code[begin] == "{":
                    member_containers.add(begin)
                    for _, operand in javascript_literal_members(code, code, begin, close + 1) or ():
                        pending.append(operand)
                else:
                    pending.extend(node_expression_parts(code, begin + 1, close))
            else:
                # Conditional/binary/comma operands stay expressions. Splitters
                # skip callable/object bodies through the same paired syntax.
                parts = node_expression_parts(code, begin, end, "?:,|&")
                if len(parts) > 1:
                    pending.extend(parts)
        for cls in re.finditer(r"\bclass(?:\s+[A-Za-z_$][\w$]*)?(?:\s+extends\s+[A-Za-z_$][\w$]*)?\s*{", code):
            member_containers.add(cls.end() - 1)
    for method in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*\(", code):
        before = method.start()
        while before and code[before - 1].isspace():
            before -= 1
        for_await = (method[1] == "await" and code[max(0, before - 3):before] == "for"
                     and (before == 3 or not re.match(r"[\w$]", code[before - 4])))
        container = brace_owner.get(method.start())
        member_role = container in member_containers
        control = method[1] in {"if", "for", "while", "switch", "catch", "with", "function"}
        property_access = before > 0 and code[before - 1] == "."
        if (control or for_await) and not member_role and not property_access:
            headers.add(method.end() - 1)
            continue
        end = node_expression_parts(code, method.end(), len(code))[-1][1]
        following = end + 1
        while following < len(code) and code[following].isspace():
            following += 1
        if code[end:end + 1] == ")" and code[following:following + 1] == "{":
            owner = body(method.end() - 1)
            if owner is not None:
                functions.add(owner)
            parameters(method.end() - 1, owner)
    for assignment in re.finditer(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*(?:=(?!=|>)|[+*/%&|^-]=|\?\?=|&&=|\|\|=)", code):
        before = assignment.start(1)
        while before and code[before - 1].isspace():
            before -= 1
        if not before or code[before - 1] != ".":
            sites.setdefault(assignment[1], set()).add(assignment.start(1))
    if _facts is not None:
        # One containment index for the binding scanner's own sites. No
        # execution states or second binding parser are maintained here.
        for match in re.finditer(r"\bclass\s+([A-Za-z_$][\w$]*)", code):
            records.append((match[1], match.start(1), None, "class"))
        for opening in tuple(ranges):
            if opening not in functions and (opening in member_containers or any(begin <= opening < end for begin, end in pattern_spans)):
                ranges.pop(opening)
        loops = []
        do_conditions = set()
        for match in re.finditer(r"(?<![\w.$])do\b\s*", code):
            begin = match.end()
            if code[begin:begin + 1] == "{" and begin in pairs:
                finish = pairs[begin] + 1
            else:
                finish = node_expression_parts(code, begin, len(code), ";\n", first=True)[0][1] + 1
            following = re.match(r"\s*while\s*\(", code[finish:])
            if following:
                opening = finish + following.end() - 1
                if opening in pairs:
                    do_conditions.add(opening)
                    loops.append((("body", begin, finish), ("condition", opening + 1, pairs[opening])))
        for opening in sorted(headers):
            if opening in do_conditions or opening not in pairs:
                continue
            before = code[:opening]
            header = re.search(r"\b(for\s*(?:await\s*)?|while)\s*$", before)
            if header is None:
                continue
            closing = pairs[opening]
            begin = closing + 1
            while begin < len(code) and code[begin].isspace():
                begin += 1
            if code[begin:begin + 1] == "{" and begin in pairs:
                finish = pairs[begin] + 1
            else:
                finish = node_expression_parts(code, begin, len(code), ";\n", first=True)[0][1] + 1
            phases = [("body", begin, finish)]
            if header[1].strip() == "while":
                phases.append(("condition", opening + 1, closing))
            else:
                parts = node_expression_parts(code, opening + 1, closing, ";")
                if len(parts) == 3:
                    phases.extend((kind, start, end) for kind, (start, end) in zip(("once", "condition", "update"), parts))
                else:
                    phases.append(("once", opening + 1, closing))
            loops.append(tuple(phases))
        # Only assignments occupying complete root operands are unconditional
        # within an update. Paired whole-expression wrappers and comma operands
        # preserve that role; logical/conditional/callable interiors do not.
        update_assignments, operands = set(), []
        for loop in loops:
            operands.extend((begin, end) for kind, begin, end in loop if kind == "update")
        while operands:
            begin, end = operands.pop()
            while True:
                while begin < end and code[begin].isspace():
                    begin += 1
                while end > begin and code[end - 1].isspace():
                    end -= 1
                if code[begin:begin + 1] == "(" and pairs.get(begin) == end - 1:
                    begin, end = begin + 1, end - 1
                else:
                    break
            if begin >= end:
                continue
            parts = node_expression_parts(code, begin, end)
            if len(parts) > 1:
                operands.extend(parts)
                continue
            target = node_expression_parts(code, begin, end, "=", first=True)[0]
            if target[1] >= end:
                continue
            finish = target[1]
            while finish > begin and code[finish - 1].isspace():
                finish -= 1
            update_assignments.add((begin, finish, target[1]))
        owners, parents, scopes = [-1] * (len(code) + 1), {}, sorted(ranges.items())
        stack, cursor = [], 0
        for position in range(len(code) + 1):
            while stack and ranges[stack[-1]] <= position:
                stack.pop()
            while cursor < len(scopes) and scopes[cursor][0] <= position:
                begin, end = scopes[cursor]
                while stack and ranges[stack[-1]] <= begin:
                    stack.pop()
                parents[begin] = stack[-1] if stack else -1
                if end > position:
                    stack.append(begin)
                cursor += 1
            owners[position] = stack[-1] if stack else -1
        bound = {}
        for name, position, override, kind in records:
            owner = owners[position] if override is None else owners[override[1]] if isinstance(override, tuple) else override
            if kind == "var":
                while owner != -1 and owner not in functions:
                    owner = parents.get(owner, -1)
            bound.setdefault(owner, {}).setdefault(name, set()).add(position)
        declared = {}
        for name, position, span in callable_declarations:
            declared.setdefault((owners[position], name), []).append(span)
        _facts.update(pairs=pairs, owners=owners, parents=parents, bound=bound, functions=functions,
                      declarations=declared, headers=headers, member_containers=member_containers,
                      targets=target_spans, expression_spans=seen, loops=tuple(loops),
                      parameter_positions=frozenset(position for _, position, _, kind in records if kind == "parameter"),
                      parameter_scopes=tuple(sorted(set(parameter_scopes), key=lambda scope: scope[1] - scope[0], reverse=True)),
                      update_assignments=frozenset(update_assignments))
    return {name: expression for name, expression in declarations.items()
            if len(sites.get(name, ())) == 1}, set(sites)



def javascript_source_literals(atoms):
    for atom in atoms:
        try:
            text = javascript_string(atom[0])
        except ValueError:
            continue
        yield text


def javascript_literal(expression, current, declarations, bindings, active=()):
    expression = expression.strip()
    # Declaration aliases are a worklist walk, not Python recursion:
    # a long but finite source alias chain must remain inspectable.
    while expression in declarations:
        if expression in active:
            return None
        start, end = declarations[expression]
        active = (*active, expression)
        expression = current[start:end].strip()
    if expression in active:
        return None
    if expression in bindings:
        return bindings[expression]
    try:
        return javascript_string(expression)
    except ValueError:
        pass
    stringify = re.fullmatch(r"JSON\.stringify\(\s*(\w+)\s*\)", expression)
    if stringify:
        operand = javascript_literal(stringify[1], current, declarations, bindings, active)
        return json.dumps(operand, ensure_ascii=False) if operand is not None else None
    if expression.startswith("`") and expression.endswith("`"):
        substitutions = list(re.finditer(r"\$\{([^{}]*)\}", expression))
        if substitutions:
            pieces, start = [], 1
            for match in substitutions:
                operand = javascript_literal(match[1], current, declarations, bindings, active)
                if operand is None:
                    return None
                try:
                    pieces.append(javascript_string("`" + expression[start:match.start()] + "`"))
                except ValueError:
                    return None
                pieces.append(operand)
                start = match.end()
            try:
                pieces.append(javascript_string("`" + expression[start:-1] + "`"))
            except ValueError:
                return None
            return "".join(pieces)
    _, projected, _, _ = javascript_projection(expression)
    parts = node_expression_parts(projected, 0, len(projected), "+")
    if len(parts) > 1:
        values = [javascript_literal(expression[start:end], current, declarations, bindings, active) for start, end in parts]
        if all(value is not None for value in values):
            return "".join(values)
    return None


def javascript_capabilities(code, native=None):
    # Static data properties do not replace the output primitive. Computed
    # global access, receiver aliases and a text member remain unknown.
    data = list(code)
    member = re.compile(r"\b(globalThis|global|window|self)\s*(?:\.\s*([A-Za-z_$][\w$]*)|\[\s*(" + JS_STRING + r")\s*\])")
    for match in member.finditer(native if native is not None else code):
        if code[match.start(1):match.end(1)] != match[1]:
            continue
        try:
            name = match[2] or javascript_string(match[3])
        except ValueError:
            continue
        if name != "text":
            data[match.start():match.end()] = name.ljust(match.end() - match.start())
    return bool(re.search(r"\b(?:tools|exec_command|apply_patch|globalThis|global|window|self)\b", "".join(data)))


def javascript_output_only(code, bound, capabilities=None):
    if capabilities is None:
        capabilities = javascript_capabilities(code)
    return (not capabilities and "text" not in bound
            and all(call[1] == "text" and not code[:call.start()].rstrip().endswith(".")
                    for call in re.finditer(r"([A-Za-z_$][\w$]*)\s*\(", code)))


def javascript_call_inputs(code, regions, *, receivers=True):
    """Select possible call operands, never unrelated fixture declarations."""
    source_starts = {start for start, _ in regions}
    for call in re.finditer(r"([A-Za-z_$][\w$]*|\]|\))\s*(?:\?\.)?\s*\(", code):
        if call.end() in source_starts:
            continue
        if call[1] in {"if", "for", "while", "switch", "catch", "with", "return", "throw", "typeof", "void", "new", "delete", "yield", "await"}:
            before = call.start()
            while before and code[before - 1].isspace():
                before -= 1
            if not before or code[before - 1] != ".":
                continue
        yield from node_expression_parts(code, call.end(), len(code))
        # Collection methods consume their receiver as well as their actuals.
        receiver = re.search(r"(?<![\w.$])([A-Za-z_$][\w$]*(?:\s*(?:\?\.|\.)\s*[A-Za-z_$][\w$]*|\s*(?:\?\.\s*)?\[[^]\n]*\])*)\s*(?:\?\.|\.)\s*$", code[:call.start()])
        if receivers and receiver:
            yield receiver.start(1), receiver.end(1)
    for loop in re.finditer(r"\bfor\s*(?:await\s*)?\(", code):
        begin, end = node_expression_parts(code, loop.end(), len(code), ";", first=True)[0]
        iterable = re.search(r"\bof\b", code[begin:end])
        if iterable:
            yield begin + iterable.end(), end


def javascript_inactive_definitions(code, *, _direct_eval_sites):
    """Prune only named definitions with no reachable reference or direct eval."""
    definitions = []
    functions = re.finditer(r"(?:^|[;{}\n])\s*(?:async\s+)?function(?:\s*\*\s*|\s+)([A-Za-z_$][\w$]*)\s*\(", code)
    arrows = re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>\s*", code)
    for match in functions:
        closing = node_expression_parts(code, match.end(), len(code))[-1][1]
        opening = closing + 1
        while opening < len(code) and code[opening].isspace():
            opening += 1
        if code[opening:opening + 1] == "{":
            end = node_expression_parts(code, opening + 1, len(code), "", first=True)[0][1]
            if code[end:end + 1] == "}":
                definitions.append((match.start(), end + 1, match[1]))
    for match in arrows:
        opening = match.end()
        block = code[opening:opening + 1] == "{"
        end = node_expression_parts(code, opening + 1 if block else opening, len(code), "" if block else ",;\n", first=True)[0][1]
        if not block or code[end:end + 1] == "}":
            definitions.append((match.start(), end + int(block), match[1]))
    if not definitions:
        return ()
    definitions.sort(key=lambda definition: (definition[0], -definition[1]))
    names = {name for _, _, name in definitions}
    references, eval_owners = {None: set()}, set()
    words = [(match.start(), match[0]) for match in re.finditer(r"[A-Za-z_$][\w$]*", code)]
    words.extend((start, None) for start in _direct_eval_sites)
    # Assign each reference to its innermost named definition. A nested dead
    # definition's own name/body cannot activate it from an active outer body.
    stack, cursor = [], 0
    for position, word in sorted(words, key=lambda item: item[0]):
        while cursor < len(definitions) and definitions[cursor][0] <= position:
            definition = definitions[cursor]
            while stack and stack[-1][1] <= definition[0]:
                stack.pop()
            stack.append(definition)
            cursor += 1
        while stack and stack[-1][1] <= position:
            stack.pop()
        owner = stack[-1][2] if stack else None
        if word is None:
            eval_owners.add(owner)
        else:
            references.setdefault(owner, set()).add(word)
    live = names & references[None]
    while True:
        # Live direct eval may name any lexical helper through a string.
        added = names if None in eval_owners or live & eval_owners else set().union(*(references.get(name, ()) for name in live)) & names
        if added <= live:
            break
        live.update(added)
    inactive = []
    for begin, end, name in definitions:
        if name not in live and (not inactive or begin >= inactive[-1][1]):
            inactive.append((begin, end))
    return inactive


def javascript_inputs(native, atoms, code, declarations, bindings=None, *, _direct_eval_sites):
    """Possible RHS inputs are separate from single-site literal proof."""
    member = r"[A-Za-z_$][\w$]*(?:\s*(?:\?\.|\.)\s*[A-Za-z_$][\w$]*|\s*(?:\?\.\s*)?\[[^]\n]*\])*"
    references = re.compile(r"(?<![\w.$])(" + member + r")")
    assignments = re.compile(r"(?<![\w.$])(" + member + r")\s*(?:=(?!=|>)|[+*/%&|^-]=|\?\?=|&&=|\|\|=)")
    sources, globals_ = {}, []
    aggregates, projections, origins = {}, {}, {}
    qualified, uncertain, all_members, wildcards = {}, {}, {}, {}
    escaped = set()
    _, bound = javascript_bindings(code, _direct_eval_sites=_direct_eval_sites)
    starts = [atom.start() for atom in atoms]
    memo = {}
    bindings = {} if bindings is None else bindings
    inactive = javascript_inactive_definitions(code, _direct_eval_sites=_direct_eval_sites)
    inactive_starts = [begin for begin, _ in inactive]

    def active(position):
        index = bisect_left(inactive_starts, position + 1) - 1
        return index < 0 or position >= inactive[index][1]

    def literal(begin, end):
        key = native[begin:end].strip()
        if key not in memo:
            memo[key] = javascript_literal(native[begin:end], native, declarations, bindings)
        return memo[key]

    def key(begin, end):
        raw = native[begin:end].strip()
        bare = re.fullmatch(r"[A-Za-z_$][\w$]*", raw)
        if bare:
            return "local", raw
        final = re.search(r"\.\s*([A-Za-z_$][\w$]*)\s*$", raw)
        if final:
            return "member", final[1]
        bracket = re.search(r"\[([^]\n]*)\]\s*$", raw)
        if bracket:
            value = javascript_literal(bracket[1], native, declarations, {})
            if value is not None:
                return "member", value
        return None

    def aggregate(span):
        if span not in aggregates:
            members_ = javascript_literal_members(native, code, *span)
            aggregates[span] = (dict(members_) if members_ is not None
                                and all(name is not None and name != "__proto__" for name, _ in members_) else None)
        return aggregates[span]

    def origin(name, active=()):
        if name in origins:
            return origins[name]
        if name in active or name not in declarations:
            return None
        span = declarations[name]
        # Only a fresh literal allocation certifies receiver identity. A
        # call/global/parameter can alias another receiver and remains broad.
        result = span if aggregate(span) is not None else None
        if result is None:
            alias = native[slice(*span)].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", alias):
                result = origin(alias, (*active, name))
        origins[name] = result
        return result

    def location(begin, end):
        raw = native[begin:end].strip()
        receiver = re.match(r"[A-Za-z_$][\w$]*", raw)
        if receiver is None or receiver.end() == len(raw):
            return None
        path = []
        for part in re.finditer(r"\.\s*([A-Za-z_$][\w$]*)|\[([^]\n]*)\]", raw[receiver.end():]):
            if part[1] is not None:
                path.append(part[1])
            elif re.fullmatch(r"\s*\d+\s*", part[2]):
                path.append(str(int(part[2])))
            else:
                path.append(javascript_literal(part[2], native, declarations, bindings))
        return receiver[0], tuple(path)

    def projected(span, path, active=(), *, certain=False):
        selected = span, path, certain
        if span in escaped:
            return () if certain and path else (span,)
        if selected in projections:
            return projections[selected]
        if not path:
            return (span,)
        if selected in active:
            return () if certain else (span,)
        members_ = aggregate(span)
        if members_ is not None and path[0] is not None and path[0] in members_:
            result = projected(members_[path[0]], path[1:], (*active, selected), certain=certain)
        elif members_ is None:
            alias = native[slice(*span)].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", alias) and alias in declarations:
                result = projected(declarations[alias], path, (*active, selected), certain=certain)
            else:
                result = () if certain else (span,)
        else:
            result = () if certain else (span,)
        projections[selected] = result
        return result

    for assignment in assignments.finditer(code):
        selected = key(assignment.start(1), assignment.end(1))
        target = location(assignment.start(1), assignment.end(1))
        if selected is None and target is None:
            continue
        rhs = node_expression_parts(code, assignment.end(), len(code), ",;\n", first=True)[0]
        if target is None:
            sources.setdefault(selected, []).append(rhs)
        else:
            receiver_name, path = target
            allocation = origin(receiver_name) if len(path) == 1 else None
            if allocation is not None and path[0] in {None, "__proto__"}:
                escaped.add(allocation)
            fresh = allocation if allocation not in escaped else None
            if path and path[-1] is not None:
                all_members.setdefault(path[-1], []).append(rhs)
                index, property_ = (qualified, (fresh, path[-1])) if fresh is not None else (uncertain, path[-1])
                index.setdefault(property_, []).append(rhs)
            else:
                wildcards.setdefault(allocation, []).append(rhs)
        receiver = native[assignment.start(1):assignment.end(1)].strip()
        global_receiver = re.match(r"(?:globalThis|global|window|self)(?=\s*[.[])", receiver)
        if not global_receiver and target is not None:
            first = re.match(r"[A-Za-z_$][\w$]*", receiver)
            if first and first[0] in declarations:
                begin, end = declarations[first[0]]
                global_receiver = native[begin:end].strip() in {"globalThis", "global", "window", "self"}
        if global_receiver and active(assignment.start()):
            globals_.append(rhs)

    # A call receiving the global object can mutate its properties without an
    # assignment expression (Object.assign, Reflect.set, or a passed helper).
    for call in re.finditer(r"([A-Za-z_$][\w$]*|\]|\))\s*(?:\?\.)?\s*\(", code):
        parts = node_expression_parts(code, call.end(), len(code))
        if active(call.start()) and any(native[begin:end].strip() in {"globalThis", "global", "window", "self"} for begin, end in parts):
            globals_.extend(parts)

    # An opaque consumer or method may copy, inherit or mutate properties.
    # Lose only the new member precision; retain the old broad source index.
    for begin, end in javascript_call_inputs(code, ()):
        if not active(begin):
            continue
        while begin < end and code[begin].isspace():
            begin += 1
        while end > begin and code[end - 1].isspace():
            end -= 1
        while (code[begin:begin + 1] == "(" and end > begin
               and node_expression_parts(code, begin + 1, end, "", first=True)[0][1] == end - 1):
            begin, end = begin + 1, end - 1
            while begin < end and code[begin].isspace():
                begin += 1
            while end > begin and code[end - 1].isspace():
                end -= 1
        raw = native[begin:end].strip()
        target = location(begin, end)
        spans = projected(declarations[target[0]], target[1], certain=True) if target is not None and target[0] in declarations else ((begin, end),)
        for span in spans:
            name = native[slice(*span)].strip()
            allocation = origin(name) if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else None
            if allocation is None and javascript_literal_members(native, code, *span) is not None:
                allocation = span
            if allocation is not None:
                escaped.add(allocation)
    projections.clear()

    for assignment in re.finditer(r"[\]}]\s*=(?!=|>)", code):
        end, depth, begin = assignment.start() + 1, 1, assignment.start() - 1
        while begin >= 0 and depth:
            depth += (code[begin] in "]}") - (code[begin] in "[{")
            begin -= 1
        if depth or begin >= 0 and re.match(r"[\w$)\]]", code[begin]):
            continue
        rhs = node_expression_parts(code, assignment.end(), len(code), ",;\n", first=True)[0]
        for name, _ in javascript_binding_names(code, begin + 1, end):
            sources.setdefault(("local", name), []).append(rhs)
    for declaration in re.finditer(r"\b(?:function\s*\*?\s*|class\s+)([A-Za-z_$][\w$]*)", code):
        opening = code.find("{", declaration.end())
        if opening < 0:
            continue
        end = node_expression_parts(code, opening + 1, len(code), "", first=True)[0][1]
        sources.setdefault(("local", declaration[1]), []).append((opening + 1, end))

    nodes = {}
    def node(begin, end):
        if (begin, end) in nodes:
            return nodes[begin, end]
        values, children, unknown = [], [], []
        value = literal(begin, end)
        if value is not None:
            values.append((begin, end, value))
        for atom in atoms[bisect_left(starts, begin):bisect_left(starts, end)]:
            try:
                values.append((atom.start(), atom.end(), javascript_string(atom[0])))
            except ValueError:
                pass
        for reference in references.finditer(code, begin, end):
            selected = key(reference.start(1), reference.end(1))
            target = location(reference.start(1), reference.end(1))
            if target is None:
                children.extend(sources.get(selected, ()))
            else:
                receiver_name, path = target
                allocation = origin(receiver_name) if len(path) == 1 else None
                fresh = allocation if allocation not in escaped else None
                if path and path[-1] is not None:
                    if fresh is not None:
                        children.extend(qualified.get((fresh, path[-1]), ()))
                        children.extend(uncertain.get(path[-1], ()))
                    else:
                        children.extend(all_members.get(path[-1], ()))
                else:
                    children.extend(span for spans in all_members.values() for span in spans)
                children.extend(wildcards.get(None, ()))
                if fresh is not None:
                    children.extend(wildcards.get(fresh, ()))
                else:
                    children.extend(span for owner, spans in wildcards.items() if owner is not None for span in spans)
            if (selected and selected[0] == "local" and selected not in sources
                    and selected[1] in bound and selected[1] not in bindings and selected[1] not in declarations
                    and selected[1] not in {"globalThis", "global", "window", "self", "undefined", "null", "true", "false"}):
                unknown.append(selected[1])
            if selected and selected[0] == "local" and selected[1] in bindings:
                values.append((reference.start(1), reference.end(1), bindings[selected[1]]))
            # Static literal members retain their own source ranges. Unknown
            # keys and receiver identities keep possible aggregate inputs.
            if target is not None:
                receiver_name, path = target
                for span in sources.get(("local", receiver_name), ()):
                    children.extend(projected(span, path))
        nodes[begin, end] = tuple(values), tuple(dict.fromkeys(children)), tuple(unknown)
        return nodes[begin, end]

    def reached(inputs, *, unresolved=None, origins=False):
        pending, visited = list(inputs), set()
        while pending:
            selected = pending.pop()
            if selected in visited:
                continue
            visited.add(selected)
            values, children, unknown = node(*selected)
            yield from values if origins else ((begin, text) for begin, _, text in values)
            pending.extend(children)
            if unresolved is not None:
                unresolved.extend(unknown)

    def patch_inputs(inputs):
        # Identify header-reaching roots in one graph walk. Do not copy or
        # enumerate a shared aggregate closure for each unrelated call input.
        roots = list(inputs)
        pending, visited, parents, headers = list(roots), set(), {}, set()
        while pending:
            selected = pending.pop()
            if selected in visited:
                continue
            visited.add(selected)
            values, children, _ = node(*selected)
            if any(javascript_patch_input(text) for _, _, text in values):
                headers.add(selected)
            for child in children:
                parents.setdefault(child, set()).add(selected)
            pending.extend(children)
        pending = list(headers)
        while pending:
            for parent in parents.get(pending.pop(), ()):
                if parent not in headers:
                    headers.add(parent)
                    pending.append(parent)
        # Transparent aliases of one shared aggregate have one composition.
        # Independent operands never exchange fragments through a shared child.
        transparent, selected_roots = {}, set()
        for root in roots:
            if root not in headers:
                continue
            selected, trail = root, set()
            while selected not in transparent and selected not in trail:
                trail.add(selected)
                values, children, _ = node(*selected)
                if values or len(children) != 1:
                    break
                selected = children[0]
            selected = transparent.get(selected, selected)
            for alias in trail:
                transparent[alias] = selected
            selected_roots.add(selected)
        return ((root,) for root in sorted(selected_roots))

    return reached, globals_, patch_inputs



def javascript_patch_input(value):
    return re.search(r"(?m)^\*\*\* (?:Add|Update) File: ", value) is not None


def javascript_patch_fragments(inputs):
    """Compose one operand; a resolved span covers only its own header atoms."""
    inputs = sorted(dict.fromkeys(inputs))
    complete = []
    for begin, end, value in inputs:
        if "*** End Patch" in value:
            paths = {effect["path"] for effect in native_file_effects("apply_patch", value)
                     if effect["kind"] == "write"}
            if paths:
                complete.append((begin, end, paths))
    emitted, headers = set(), []
    for begin, end, value in inputs:
        if javascript_patch_input(value) and value not in emitted:
            emitted.add(value)
            yield begin, value
        for header in re.finditer(r"(?m)^\*\*\* (Add|Update) File: ([^\n]*)", value):
            paths = [header[2].strip()] if header[2].strip() else [text for _, _, text in inputs
                if re.fullmatch(r"[/\w.\-]+", text) and text not in {"", ".", "/"}]
            headers.extend((begin, header[1], path) for path in paths
                           if not any(start <= begin and end <= finish and path in paths_
                                      for start, finish, paths_ in complete))
    if not headers:
        return
    fragments = []
    for position, _, value in inputs:
        if "*** End Patch" in value and javascript_patch_input(value):
            continue
        lines = [line[1:] if line.startswith("+") else line for line in value.splitlines()
                 if not line.startswith(("*** ", "@@", "-"))]
        fragments.append((position, value, lines))
    for begin, kind, path in dict.fromkeys(headers):
        lines = [line for _, value, body in fragments if value != path for line in body]
        if lines:
            patch = "*** " + kind + " File: " + path + "\n" + "\n".join("+" + line for line in lines)
            if patch not in emitted:
                emitted.add(patch)
                yield begin, patch


def javascript_possible_patches(native, *, capabilities=False):
    """Unbound call targets may consume their patch inputs, as partial writes."""
    projection_facts = {}
    atoms, code, regions, _ = javascript_projection(native, _facts=projection_facts)
    source_calls = list(javascript_source_calls(native, code,
        literal_atoms=atoms, templates=projection_facts["templates"]))
    direct_eval_sites = tuple(start for start, _, _, direct, _ in source_calls if direct)
    declarations, bound = javascript_bindings(code, _direct_eval_sites=direct_eval_sites)
    if javascript_output_only(code, bound, capabilities or javascript_capabilities(code, native)):
        return
    seen = set()
    for _, raw in composed_patch_fields(native):
        try:
            seen.add(composed_string(raw, native))
        except (SyntaxError, ValueError):
            pass
    reached, _, patch_inputs = javascript_inputs(native, atoms, code, declarations,
        _direct_eval_sites=direct_eval_sites)
    operands = patch_inputs(javascript_call_inputs(code, regions))
    # A shared aggregate is walked once across all header-reaching operands.
    # Keep origin tuples; repeated statements at distinct origins still matter.
    for group in operands:
        for begin, patch in javascript_patch_fragments(reached(group, origins=True)):
            if patch not in seen:
                seen.add(patch)
                yield begin, patch


def javascript_generated_sources(value):
    """Discover generated code once, carrying only reached literal bindings.

    This is source discovery, never execution or command/result binding. The
    shared projection separates source arguments from inert fixtures and the
    existing delimiter splitter joins known literal source fragments. Unknown
    computation keeps its selected operands as possible command input.
    """
    root_capabilities = javascript_capabilities(javascript_projection(value)[1], value)
    deferred = []
    pending, seen, queued = [(value, {}, (), False, (), False, False)], set(), {(value, (), (), (), False, False)}
    generated_literals, writer_cache = [], {}
    patch_literals = []
    while pending:
        current, inherited, inherited_globals, generated, fallback, sloppy, global_this = pending.pop()
        projection_facts = {}
        current_atoms, current_code, regions, _ = javascript_projection(current, _facts=projection_facts)
        local_capabilities = javascript_capabilities(current_code, current) or global_this and bool(re.search(r"\bthis\b", current_code))
        root_capabilities |= local_capabilities
        source_calls = list(javascript_source_calls(current, current_code, global_this=global_this, literal_atoms=current_atoms, templates=projection_facts["templates"]))
        direct_eval_sites = tuple(start for start, _, _, direct, _ in source_calls if direct)
        declarations, bound = javascript_bindings(current_code, sloppy=sloppy, _direct_eval_sites=direct_eval_sites)
        bindings = {name: text for name, text in inherited.items() if name not in bound and not direct_eval_sites}

        def literal(expression, active=()):
            return javascript_literal(expression, current, declarations, bindings, active)

        for name in declarations:
            text = literal(name)
            if text is not None:
                bindings[name] = text
        reached_inputs, global_ranges, _ = javascript_inputs(current, current_atoms, current_code, declarations, bindings,
            _direct_eval_sites=direct_eval_sites)
        unknown_globals = []
        global_values = [text for _, text in reached_inputs(global_ranges, unresolved=unknown_globals)]
        if unknown_globals:
            # A reached global mutation can consume a loop/callback parameter.
            # No global write means module literals remain local and inert.
            global_values.extend(javascript_source_literals(current_atoms))
        global_values = tuple(dict.fromkeys((*inherited_globals, *global_values)))
        global_context = global_values
        context = tuple(sorted(bindings.items()))
        key = current, context, global_context, fallback, sloppy, global_this
        if key in seen:
            continue
        seen.add(key)
        if generated:
            generated_literals.extend(global_values)
            generated_literals.extend(javascript_source_literals(current_atoms))
            patch_literals.extend(value for value in javascript_source_literals(current_atoms) if javascript_patch_input(value))
            text, projected = current, current_code
            replacements = []
            for match in re.finditer(r"(\b(?:cmd|command)\s*:\s*|\btools\.apply_patch\s*\(\s*)((?:(?:globalThis|global|window|self)\s*\.\s*)?(\w+))\b", projected):
                operands = {} if "." in match[2] else bindings
                end = node_expression_parts(projected, match.start(2), len(projected), first=True)[0][1]
                operand = literal(current[match.start(2):end])
                if operand is not None:
                    replacements.append((match.start(2), end, json.dumps(operand)))
                elif match[3] in operands:
                    replacements.append((match.start(2), match.end(2), json.dumps(operands[match[3]])))
            for match in re.finditer(r"([\"'])(cmd|command)\1\s*:\s*((?:(?:globalThis|global|window|self)\s*\.\s*)?(\w+))\b", text):
                if projected[match.start(3):match.end(3)] != match[3]:
                    continue
                operands = {} if "." in match[3] else bindings
                end = node_expression_parts(projected, match.start(3), len(projected), first=True)[0][1]
                operand = literal(current[match.start(3):end])
                if operand is not None:
                    replacements.append((match.start(3), end, json.dumps(operand)))
                elif match[4] in operands:
                    replacements.append((match.start(3), match.end(3), json.dumps(operands[match[4]])))
            for match in re.finditer(r"[{,]\s*(cmd|command)\s*(?=[,}])", projected):
                if match[1] in bindings:
                    replacements.append((match.start(1), match.end(1), match[1] + ":" + json.dumps(bindings[match[1]])))
            for begin, finish in javascript_call_inputs(projected, regions):
                operand = literal(current[begin:finish])
                if operand is not None and javascript_patch_input(operand):
                    replacements.append((begin, finish, json.dumps(operand)))
                    continue
                for match in re.finditer(r"(?<![\w.$])([A-Za-z_$][\w$]*)(?![\w$]|\s*:)", projected[begin:finish]):
                    start, end = begin + match.start(), begin + match.end()
                    operand = literal(current[start:end])
                    if operand is not None and javascript_patch_input(operand):
                        replacements.append((start, end, json.dumps(operand)))
            # All spans refer to the original source. A containing operand wins
            # over an identifier within it, before any replacement is applied.
            accepted = []
            for replacement in sorted(set(replacements), key=lambda item: (item[0], -item[1])):
                start, end, _ = replacement
                if not accepted or start >= accepted[-1][1]:
                    accepted.append(replacement)
            for start, end, replacement in sorted(accepted, reverse=True):
                text = text[:start] + replacement + text[end:]
            yield text
        parsed_patches = {position for position, _ in composed_patch_fields(text if generated else current)}
        unknown_patch = any(call.start() not in parsed_patches
                            for call in javascript_call_starts(text if generated else current, r"\btools\.apply_patch\s*\("))
        unknown_inputs = any(current[begin:finish].strip() and literal(current[begin:finish]) is None
                             for begin, finish in javascript_call_inputs(current_code, regions, receivers=False))
        deferred.append((text if generated else current, current_code, bound, unknown_patch, unknown_inputs, fallback, sloppy, global_this))
        selected, visited, sources = [], set(), []
        for start, end, name, direct, parts in source_calls:
            constructor = name == "constructor"
            child_bindings = dict(bindings) if direct else {}
            child_fallback = list(dict.fromkeys((*fallback, *javascript_source_literals(current_atoms)))) if direct else list(global_values)
            if constructor:
                # Function constructors have their global scope and explicit
                # parameters, never the enclosing module's lexical bindings.
                following = end + 1
                while following < len(current_code) and current_code[following].isspace():
                    following += 1
                actuals = []
                immediate = following < len(current_code) and current_code[following] == "("
                if immediate:
                    actuals = [(begin, finish) for begin, finish in node_expression_parts(current_code, following + 1, len(current_code))
                               if current[begin:finish].strip()]
                assigned = re.search(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:new\s+)?[\w.$]+\s*\(\s*$", current_code[:start])
                invocations = [actuals] if immediate else []
                complete_invocations = immediate
                if assigned:
                    call_names = {assigned[1]}
                    for _ in declarations:
                        added = {name for name, (begin, finish) in declarations.items()
                                 if current[begin:finish].strip() in call_names}
                        if added <= call_names:
                            break
                        call_names.update(added)
                    invocations = []
                    covered = {assigned.start(1)}
                    for alias in call_names - {assigned[1]}:
                        for declaration in re.finditer(r"\b(?:const|let|var)\s+(" + re.escape(alias) + r")\s*=\s*([A-Za-z_$][\w$]*)", current_code):
                            if declaration[2] in call_names:
                                covered.update((declaration.start(1), declaration.start(2)))
                    for invocation in re.finditer(r"(?<![\w.$])(?:" + "|".join(map(re.escape, call_names)) + r")\s*\(", current_code):
                        covered.add(invocation.start())
                        invocations.append([(begin, finish) for begin, finish in node_expression_parts(current_code, invocation.end(), len(current_code))
                                            if current[begin:finish].strip()])
                    complete_invocations = all(reference.start() in covered for reference in re.finditer(
                        r"(?<![\w.$])(?:" + "|".join(map(re.escape, call_names)) + r")(?![\w$])", current_code))
                reached = [part for invocation in invocations for part in invocation]
                visited_actuals = set()
                while reached:
                    begin, finish = reached.pop()
                    if (begin, finish) in visited_actuals:
                        continue
                    visited_actuals.add((begin, finish))
                    child_fallback.extend(javascript_source_literals(atom for atom in current_atoms if begin <= atom.start() < finish))
                    for word in re.findall(r"\b\w+\b", current_code[begin:finish]):
                        if word in declarations:
                            reached.append(declarations[word])
                        if word in bindings:
                            child_fallback.append(bindings[word])
                parameters = [literal(current[begin:finish]) for begin, finish in parts[:-1]]
                parameter_parts = []
                for parameter in parameters:
                    if parameter is None:
                        continue
                    _, parameter_code, _, _ = javascript_projection(parameter)
                    parameter_parts.extend(parameter[begin:finish].strip()
                                           for begin, finish in node_expression_parts(parameter_code, 0, len(parameter)))
                for parameter in parameter_parts:
                    for parameter_name in re.findall(r"\b[A-Za-z_$][\w$]*\b", parameter):
                        child_bindings.pop(parameter_name, None)
                complete_actuals = all(literal(current[begin:finish]) is not None
                                       for actuals in invocations for begin, finish in actuals)
                complete_formals = all(parameter is not None for parameter in parameters)
                plain_parameters = complete_formals and all(re.fullmatch(r"([A-Za-z_$][\w$]*)(?:\s*=\s*([\s\S]*))?", parameter)
                                       for parameter in parameter_parts)
                if not complete_invocations or not complete_actuals or not plain_parameters:
                    child_fallback.extend((*fallback, *javascript_source_literals(current_atoms)))
                for index, parameter in enumerate(parameter_parts):
                    named = re.fullmatch(r"([A-Za-z_$][\w$]*)(?:\s*=\s*([\s\S]*))?", parameter)
                    if named is None:
                        continue
                    values = []
                    for actuals in invocations or [[]]:
                        expression = current[slice(*actuals[index])].strip() if index < len(actuals) else "undefined"
                        operand = literal(expression)
                        if expression == "undefined" and named[2] is not None:
                            try:
                                operand = javascript_string(named[2])
                            except ValueError:
                                operand = None
                        values.append(operand)
                        if operand is not None:
                            child_fallback.append(operand)
                    if complete_formals and complete_invocations and values[0] is not None and all(value == values[0] for value in values):
                        child_bindings[named[1]] = values[0]
                    else:
                        # Loops, callbacks and reflective calls are not covered
                        # by a direct-call list. Their parameters stay unknown.
                        child_fallback.extend((*fallback, *javascript_source_literals(current_atoms)))
                parts = parts[-1:]
            else:
                parts = parts[:1]
            selected.extend((begin, finish, child_bindings, tuple(dict.fromkeys(child_fallback)),
                             name != "import" and (not direct or sloppy),
                             global_this if direct else True if name == "eval" else None if constructor else False)
                            for begin, finish in parts)
        while selected:
            start, end, child_bindings, child_fallback, child_sloppy, child_global_this = selected.pop()
            selected_key = start, end, tuple(sorted(child_bindings.items())), child_fallback, child_sloppy, child_global_this
            if selected_key in visited:
                continue
            visited.add(selected_key)
            parts = node_expression_parts(current_code, start, end)
            for begin, finish in parts:
                expression = current[begin:finish]
                text = literal(expression)
                if text is not None:
                    sources.append((text, False, child_bindings, child_fallback, child_sloppy, child_global_this))
                    continue
                # An unresolved source expression may concatenate or transform
                # selected literals. Preserve those inputs, not unrelated data.
                reached = [declarations[word] for word in re.findall(r"\b\w+\b", current_code[begin:finish])
                           if word in declarations]
                selected.extend((begin, finish, child_bindings, child_fallback, child_sloppy, child_global_this) for begin, finish in reached)
                for word in re.findall(r"\b\w+\b", current_code[begin:finish]):
                    if word in bindings:
                        sources.append((bindings[word], True, child_bindings, child_fallback, child_sloppy, child_global_this))
                for atom in current_atoms:
                    if begin <= atom.start() < finish:
                        try:
                            sources.append((javascript_string(atom[0]), True, child_bindings, child_fallback, child_sloppy, child_global_this))
                        except ValueError:
                            pass
        for text, uncertain, child_bindings, child_fallback, child_sloppy, child_global_this in sources:
            if re.match(r"\s*([\"'])use strict\1\s*(?:;|$)", text):
                child_sloppy = False
            # Constructors derive this from their own strictness; indirect eval
            # keeps the global this even when its source is explicitly strict.
            if child_global_this is None:
                child_global_this = child_sloppy
            child_context = tuple(sorted(child_bindings.items()))
            nested_key = text, child_context, global_context, child_fallback, child_sloppy, child_global_this
            if nested_key not in queued:
                queued.add(nested_key)
                pending.append((text, child_bindings, global_values, True, child_fallback, child_sloppy, child_global_this))
            if uncertain:
                # Computed source may turn a selected literal into shell or
                # patch input. These alternatives affect discovery only; they
                # cannot bind a native result or attest completion.
                carrier = "apply_patch(" + json.dumps(text) + ")" if javascript_patch_input(text) else "tools.exec_command({cmd:" + json.dumps(text) + "})"
                if (carrier, child_context, global_context) not in queued:
                    queued.add((carrier, child_context, global_context))
                    yield carrier

    # Output purity belongs to the whole reachable cell. Evaluate it only after
    # every parent and sibling source has contributed its possible capability.
    writer_values = tuple(dict.fromkeys(value for value in generated_literals
        if executable_content_cached(value, writer_cache)))
    patch_values = tuple(dict.fromkeys(patch_literals))
    emitted_commands, emitted_patches, uncertain_writers = set(), set(), False
    for text, projected, bound, unknown_patch, unknown_inputs, fallback, sloppy, global_this in deferred:
        capabilities = root_capabilities or global_this and bool(re.search(r"\bthis\b", projected))
        possible_patches = tuple(javascript_possible_patches(text, capabilities=capabilities)) if global_this else ()
        for _, patch in possible_patches:
            if patch not in emitted_patches:
                emitted_patches.add(patch)
                yield "apply_patch(" + json.dumps(patch) + ")"
        if unknown_patch and unknown_inputs:
            inputs = tuple((index, index + 1, value) for index, value in enumerate((*patch_values, *fallback)))
            for _, patch in javascript_patch_fragments(inputs):
                if patch not in emitted_patches:
                    emitted_patches.add(patch)
                    yield "apply_patch(" + json.dumps(patch) + ")"
        if not fallback and not (sloppy or global_this):
            continue
        unknown_patch |= not javascript_output_only(projected, bound, capabilities) and unknown_inputs
        command, unknown_command = _unparsed_command_text(text, generated_sources=False, capabilities=capabilities,
                                                         sloppy=sloppy, writer_cache=writer_cache)
        commands = [command] if executable_content_cached(command, writer_cache) else []
        uncertain_writers |= (sloppy or global_this) and unknown_command
        for command in dict.fromkeys(commands if sloppy or global_this else ()):
            # Context-sensitive uncertainty must survive the public string-only
            # generated-source API. This is discovery, never a native receipt.
            if command not in emitted_commands:
                emitted_commands.add(command)
                yield "tools.exec_command({cmd:" + json.dumps(command) + "})"
        for operand in fallback:
            patch = javascript_patch_input(operand)
            if (unknown_patch if patch else unknown_command):
                emitted = emitted_patches if patch else emitted_commands
                if operand not in emitted:
                    emitted.add(operand)
                    yield ("apply_patch(" + json.dumps(operand) + ")" if patch
                           else "tools.exec_command({cmd:" + json.dumps(operand) + "})")
    if uncertain_writers:
        for command in writer_values:
            if command not in emitted_commands:
                emitted_commands.add(command)
                yield "tools.exec_command({cmd:" + json.dumps(command) + "})"


def javascript_call_starts(native, pattern):
    atoms, _, _, _ = javascript_projection(native)
    cursor = 0
    for start in re.finditer(pattern, native):
        while cursor < len(atoms) and atoms[cursor].end() <= start.start():
            cursor += 1
        if cursor < len(atoms) and atoms[cursor].start() <= start.start() < atoms[cursor].end():
            continue
        yield start


def composed_shell_fields(native):
    """Executable call starts never come from established inert atoms."""
    for start in javascript_call_starts(native, r"\btools\.exec_command\s*\("):
        if match := SHELL_FIELD.match(native, start.start()):
            yield match


def composed_patch_fields(native):
    for start in javascript_call_starts(native, r"\btools\.apply_patch\s*\("):
        if match := re.match(rf"\s*({JS_STRING})", native[start.end():]):
            yield start.start(), match[1]


def composed_string(raw, native):
    if raw.startswith("String.raw") and not composed_calls(native):
        raise ValueError("String.raw tag is not established by a literal cell")
    return javascript_string(raw)


def shell_commands(name, value, *, depth=0):
    """Only shell tools execute commands; tool prompts and echo operands do not."""
    def commands(argv):
        child = unwrapped_command(argv)
        body = shell_c_body(child)
        if depth < 16 and body is not None and foreground_shell_text(body):
            yield from shell_commands("exec_command", {"cmd": body, "shell": child[0]}, depth=depth + 1)
        else:
            yield argv[1:] if argv[0] == "nohup" else argv
    for text in shell_texts("exec_command" if name == "functions.exec_command" else name, value):
        try:
            reached = []
            shell_code(text, commands=reached, source_shell=arguments(value).get("shell") if name in {"exec_command", "functions.exec_command"} and isinstance(arguments(value), dict) else None)
            for argv in reached:
                yield from commands(argv)
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


def node_expression_parts(code, start, end, separators=",", *, first=False):
    """Split actual JS expressions at balanced, unquoted delimiters."""
    parts, stack, begin = [], [], start
    for cursor in range(start, end):
        token = code[cursor]
        if not stack and token in separators:
            if first:
                return [(begin, cursor)]
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
                                colon = node_expression_parts(code, at + label.end(), body["end"] - 1, ":", first=True)[0][1]
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
        end = node_expression_parts(code, cursor, len(code), ";\n", first=True)[0][1]
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
            begin, end = node_expression_parts(code, body, len(code), ";,\n", first=True)[0]
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
        _, end = node_expression_parts(code, start, len(code), ";,\n", first=True)[0]
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


def compound_bounds(tokens, *, completion=False):
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
        command_start = bool(token and all(c in ";&|\n" for c in token)
                             or token in {"do", "then", "else", "if", "elif", "while", "until", "!", "(", "{"})
    if completion:
        # A physical newline inside a compound or after a pipeline/and-or
        # operator does not finish the shell's current input unit.
        tail = next((token for token in reversed(tokens) if token.strip("\n")), "")
        return bounds, not stack and tail not in {"&&", "||", "|", "|&", "<", ">", "<<", ">>", "<<<", "<>", "<&", ">&", ">|", "&>", "&>>"}
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
    heredoc_languages(list(lexer), lexer._raw_source, command_kinds=commands, stdin_kinds=kinds)
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


def source_mutation_paths(argv):
    """Ordered mutations of recorded source; never infer writes from readers."""
    child = unwrapped_command(argv)
    if not child or any(word in {"--help", "--version", "--dry-run"} for word in child[1:]):
        return []
    name, words = Path(child[0]).name, child[1:]
    def invalidation(path, **extra):
        return {"path": path, "invalidates": True, "recursive": True, **extra}
    if name in {"cp", "mv", "install"}:
        operands, target, cursor = [], None, 0
        recursive = name == "mv" or any(word in {"--recursive", "--archive"} or word.startswith('-') and not word.startswith('--')
                                        and any(letter in word[1:] for letter in "rRa") for word in words)
        def transfer(source, destination, **extra):
            return invalidation(destination, copy_from=source, replaces=True, copy_recursive=recursive,
                                copy_contents=source.rstrip('/').endswith('/.'), **extra)
        while cursor < len(words):
            word = words[cursor]
            if name == "install" and word in {"-m", "--mode", "-o", "--owner", "-g", "--group", "--suffix"}:
                cursor += 2
                continue
            if word in {"-t", "--target-directory"} and cursor + 1 < len(words):
                target, cursor = words[cursor + 1], cursor + 2
                continue
            if word.startswith("--target-directory="):
                target = word.partition("=")[2]
            elif word == "--":
                operands.extend(words[cursor + 1:])
                break
            elif not word.startswith("-"):
                operands.append(word)
            cursor += 1
        if target is not None:
            return [transfer(path, posixpath.join(target, posixpath.basename(path.rstrip('/'))))
                    for path in operands]
        if len(operands) >= 2:
            destination = operands[-1]
            return [transfer(path, destination, destination_may_be_directory=True, destination_is_directory=len(operands) > 2)
                    for path in operands[:-1]]
    if name in {"sed", "perl"}:
        if any(word.startswith("-") and not word.startswith("--") and "i" in word[1:]
               or word == "--in-place" or word.startswith("--in-place=") for word in words):
            return [invalidation(word) for word in words if not word.startswith("-")]
        return []
    if name == "patch":
        directory = "."
        for index, word in enumerate(words):
            if word in {"-d", "--directory"} and index + 1 < len(words):
                directory = words[index + 1]
            elif word.startswith("--directory="):
                directory = word.partition("=")[2]
        return [invalidation(directory)]
    if name == "git":
        directory = ""
        while words and words[0].startswith("-"):
            option = words.pop(0)
            if option in {"-C", "-c", "--git-dir", "--work-tree"} and words:
                value = words.pop(0)
                if option in {"-C", "--work-tree"}:
                    directory = posixpath.join(directory, value)
            elif option.startswith("--work-tree="):
                directory = option.partition("=")[2]
        if not words:
            return []
        operation, words = words[0], words[1:]
        if operation == "checkout" and "--" in words:
            return [invalidation(posixpath.join(directory, path)) for path in words[words.index("--") + 1:]]
        short = {letter for word in words if word.startswith("-") and not word.startswith("--")
                 for letter in word[1:]}
        staged = "--staged" in words or "S" in short
        worktree = "--worktree" in words or "W" in short
        if operation == "restore" and not (staged and not worktree):
            if any(word.startswith("--pathspec-from-file") for word in words):
                return [invalidation(directory or ".")]
            paths, cursor = [], 0
            while cursor < len(words):
                word = words[cursor]
                if word == "--":
                    paths.extend(words[cursor + 1:])
                    break
                if word in {"-s", "--source", "--pathspec-from-file"}:
                    cursor += 2
                    continue
                if not word.startswith("-"):
                    paths.append(word)
                cursor += 1
            return [invalidation(posixpath.join(directory, path)) for path in paths]
        if operation in {"checkout", "switch", "pull", "merge", "rebase", "cherry-pick", "revert", "apply", "am"} or (
                operation == "reset" and {"--hard", "--merge", "--keep"}.intersection(words)) or (
                operation == "stash" and (not words or words[0] in {"pop", "apply", "push", "save", "branch"})):
            return [invalidation(directory or ".")]
    return []


def shell_literal_status(words):
    """Use only the existing literal guard facts; never execute a command."""
    words = [word for word in words if word not in {";", "\n"}]
    while words and words[0] in {"do", "then", "else", "{", "("}:
        words = words[1:]
    return True if words in (["true"], [":"]) else False if words == ["false"] else None


def heredoc_languages(tokens, source, *, file_commands=None, command_kinds=None, inline_inputs=None,
                      stream_sources=None, output_sources=None, execution_events=None, stdin_kinds=None,
                      success_facts=None, source_bodies=None, source_shell=None, literal_source=None):
    """Parse command descriptors once and carry each stdin source through groups.

    A balanced group is one outer pipeline node. Its children share the same
    parser, so inner separators and redirections cannot be borrowed by siblings.
    Source sets preserve distinct bodies without concatenating unrelated data.
    """
    root_stop = len(tokens)
    source_at = [source] * root_stop
    source_bodies = {} if source_bodies is None else source_bodies
    spans, cursor = [], 0
    for token in tokens:
        start = source.find(token, cursor)
        spans.append((start, start + len(token)) if start >= 0 else (None, None))
        if start >= 0:
            cursor = start + len(token)
    bounds = compound_bounds(tokens)
    definitions = {}

    def register_definitions(first, last):
        for index in range(first, last):
            token = tokens[index]
            if not re.fullmatch(r"[A-Za-z_]\w*", token):
                continue
            if tokens[index + 1:index + 3] == ["(", ")"]:
                name, body = token, index + 3
            elif token == "function" and index + 1 < last and re.fullmatch(r"[A-Za-z_]\w*", tokens[index + 1]):
                name, body = tokens[index + 1], index + 2
                if tokens[body:body + 2] == ["(", ")"]:
                    body += 2
            else:
                continue
            while body < last and tokens[body] in {";", "\n"}:
                body += 1
            if body in bounds:
                stop = bounds[body] + 1
            else:
                stop = body
                while stop < last and not all(c in ";&|\n" for c in tokens[stop]):
                    stop = bounds.get(stop, stop) + 1
            if body < stop:
                definitions[index] = (name, body, stop)

    register_definitions(0, root_stop)
    stream_sources = {} if stream_sources is None else stream_sources
    languages = {}

    def embedded(body, shell, incremental):
        try:
            code, child_tokens, bodies, complete = shell_source(body, incremental=incremental)
        except ValueError:
            return None  # A malformed nested body cannot discard its caller.
        # One position domain, but embedded roots are entered only by their caller.
        scope = len(tokens)
        tokens.append(";")
        spans.append((None, None))
        source_at.append(None)
        begin = len(tokens)
        tokens.extend(child_tokens)
        source_at.extend([code] * len(child_tokens))
        cursor = 0
        for token in child_tokens:
            start = code.find(token, cursor)
            spans.append((start, start + len(token)) if start >= 0 else (None, None))
            if start >= 0:
                cursor = start + len(token)
        stop = len(tokens)
        tokens.append(";")
        spans.append((None, None))
        source_at.append(None)
        bounds.update((begin + left, begin + right) for left, right in compound_bounds(child_tokens).items())
        source_bodies.update((begin + at, value) for at, value in bodies.items())
        register_definitions(begin, stop)
        return scope, begin, stop, parse(begin, stop, shell), complete

    def parse(begin, end, shell):
        segments, current, index = [], [], begin
        while index < end:
            token = tokens[index]
            if index in definitions and definitions[index][2] <= end:
                stop = definitions[index][2]
                current.extend(range(index, stop))
                index = stop
                continue
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
            argv, heredocs, children, child_scope, cursor, last_word = [], [], None, None, 0, None
            substitutions = {}
            word_literals = []
            descriptors = {0: "pipe-in", 1: "pipe-out", 2: None}
            while cursor < len(segment):
                index, token = segment[cursor], tokens[segment[cursor]]
                if (token in {"<", ">"} and cursor + 1 < len(segment)
                        and tokens[segment[cursor + 1]] == "(" and segment[cursor + 1] in bounds
                        and spans[index][1] == spans[segment[cursor + 1]][0]):
                    opening = segment[cursor + 1]
                    close = bounds[opening]
                    placeholder = f"PROCESS_SUBSTITUTION_{index}"
                    substitutions[placeholder] = (token, parse(opening + 1, close, shell))
                    argv.append(placeholder)
                    word_literals.append(False)
                    cursor = segment.index(close) + 1
                    continue
                if token in {"(", "{"} and index in bounds and bounds[index] in segment:
                    close = bounds[index]
                    children, child_scope = parse(index + 1, close, shell), token
                    cursor = segment.index(close) + 1
                    continue
                if token in {"<", ">", "<<", ">>", "<<<", "<>", "<&", ">&", ">|", "&>", "&>>"}:
                    fd = 0 if token.startswith("<") else 1
                    previous = segment[cursor - 1] if cursor else None
                    if (previous == last_word and previous is not None and tokens[previous].isdigit()
                            and spans[previous][1] == spans[index][0]):
                        fd = int(tokens[previous])
                        argv.pop()
                        word_literals.pop()
                    operand = tokens[segment[cursor + 1]] if cursor + 1 < len(segment) else ""
                    if (cursor + 2 < len(segment) and operand in {"<", ">"}
                            and tokens[segment[cursor + 2]] == "(" and segment[cursor + 2] in bounds
                            and spans[segment[cursor + 1]][1] == spans[segment[cursor + 2]][0]):
                        opening = segment[cursor + 2]
                        close = bounds[opening]
                        descriptors[fd] = ("process-input" if fd == 0 else "process-output", parse(opening + 1, close, shell))
                        cursor = segment.index(close) + 1
                        continue
                    if token in {"<<", "<<<"}:
                        heredocs.append(index)
                        languages[index] = None
                        descriptors[fd] = index
                        if token == "<<<" and inline_inputs is not None:
                            decoded = GroupShellLexer.words(operand)
                            if len(decoded) == 1:
                                inline_inputs[index] = (operand, decoded[0] + "\n")
                    elif token in {"<&", ">&"} and operand.isdigit():
                        descriptors[fd] = descriptors.get(int(operand))
                    elif token == "<":
                        target = GroupShellLexer.words(operand)
                        descriptors[fd] = ("input-file", target[0]) if len(target) == 1 else None
                    elif token in {">", ">>", ">|", "&>", "&>>"}:
                        target = GroupShellLexer.words(operand)
                        descriptors[fd] = ("file", target[0], token not in {">>", "&>>"}, index) if len(target) == 1 else None
                        if token.startswith("&>"):
                            descriptors[2] = descriptors[fd]
                    else:
                        descriptors[fd] = None
                    cursor += 2
                    continue
                decoded = GroupShellLexer.words(token)
                argv.extend(decoded)
                word_literals.extend([shell_word_literal(token)] * len(decoded))
                last_word = index
                cursor += 1
            definition = definitions.get(segment[0])
            if definition is not None:
                name, body, stop = definition
                argv, children, child_scope = [name], parse(body, stop, shell), None
                word_literals = [True]
                descriptors = {0: "pipe-in", 1: "pipe-out", 2: None}
            if any(tokens[opening] == "case" and opening <= segment[0] < closing for opening, closing in bounds.items()):
                pattern_end = next((at for at in segment if tokens[at] == ")"), None)
                if pattern_end is not None:
                    # The pattern and `case word in` prefix do not belong to
                    # the arm's command or to a literal producer's output.
                    prefix = []
                    for at in segment:
                        prefix.extend(GroupShellLexer.words(tokens[at]))
                        if at == pattern_end:
                            break
                    if argv[:len(prefix)] == prefix:
                        argv = argv[len(prefix):]
                        word_literals = word_literals[len(prefix):]
            inline_body, body_scope, fresh_shell, child_shell = None, None, False, None
            malformed_body = False
            body_complete = True
            if children is None and definition is None:
                action, _, _, _ = shell_command_action(argv)
                child = unwrapped_command(action, unwrap_code=False)
                body = None
                if action[:1] == ["eval"] and all(word_literals[len(argv) - len(action) + 1:]):
                    body = " ".join(action[1:]) if len(action) > 1 else None
                elif action[:1] == ["emulate"]:
                    bodies = list(executor_commands(action))
                    body = bodies[0] if len(bodies) == 1 else None
                    if body is not None and not word_literals[argv.index(body, 1)]:
                        body = None
                elif shell_c_body(child) is not None and argv[-len(child):] == child:
                    child_body = shell_c_body(child)
                    if word_literals[len(argv) - len(child) + child.index(child_body, 1)]:
                        body, fresh_shell, child_shell = child_body, True, Path(child[0]).name
                if body is not None:
                    context = child_shell if fresh_shell else shell
                    parsed = embedded(body, context, context != "zsh")
                    if parsed is not None:
                        body_scope, child_begin, child_end, children, body_complete = parsed
                        inline_body = (child_begin, child_end, body_scope)
                    else:
                        body_complete = False
                    malformed_body = not body_complete
            language = None if malformed_body else stdin_language(argv)
            input_positions = None
            redirection_owner = segment[0]
            if children is None and tokens[segment[0]] in {"}", ")", "done", "fi", "esac"}:
                language = compound_stdin_language(tokens, source_at[segment[0]], segment[0], spans, heredocs) or language
                opening = next((begin for begin, close in bounds.items() if close == segment[0]), None)
                if opening is not None:
                    redirection_owner = opening
                if opening is not None and language:
                    def consumers(prior):
                        for candidate in prior:
                            if candidate["children"] is not None:
                                yield from consumers(candidate["children"])
                            elif opening <= candidate["index"] < segment[0]:
                                child = unwrapped_command(candidate["argv"], unwrap_code=False)
                                if candidate["language"] or language == "shell" and child[:1] == ["eval"]:
                                    yield candidate["index"]
                    input_positions = tuple(consumers(nodes))
            nodes.append({"argv": argv, "children": children, "child_scope": child_scope, "definition": definition,
                          "language": language, "index": segment[0], "stop": segment[-1] + 1,
                          "inline_body": inline_body, "body_scope": body_scope, "fresh_shell": fresh_shell, "child_shell": child_shell, "input_positions": input_positions, "redirection_owner": redirection_owner,
                          "body_complete": body_complete,
                          "shell_context": shell, "stdin_body": False,
                          "descriptors": descriptors, "heredocs": heredocs,
                          "substitutions": substitutions,
                          "separator": separator, "source": " ".join(tokens[index] for index in segment)})
        return nodes

    def source_leaves(identifier):
        stream = stream_sources.get(identifier, {})
        if stream.get("kind") in {"choice", "concat"}:
            for part in stream["parts"]:
                yield from source_leaves(part)
        else:
            yield identifier

    def assign_language(sources, language, position=None):
        if language:
            leaves = set(source for identifier in sources for source in source_leaves(identifier))
            if stdin_kinds is not None and "external" in leaves:
                stdin_kinds.add(language)
            executable_sources = set(sources)
            if any(stream_sources.get(source, {}).get("kind") == "file" for source in leaves):
                # Unknown file bytes cannot prove the connected literal input
                # inert. Fully known concatenations retain their exact bytes.
                executable_sources.update(source for source in leaves
                                          if source != "external" and stream_sources.get(source, {}).get("kind") != "file")
            for identifier in executable_sources:
                if identifier != "external":
                    previous = languages.get(identifier)
                    languages[identifier] = language if previous in {None, language} else "argv"
            if execution_events is not None:
                execution_events.extend({"kind": "stdin", "position": position, "source": identifier,
                                         "language": language, "command_effect": True}
                                        for identifier in executable_sources if identifier != "external")
                paths = [stream_sources[source]["path"] for source in leaves
                         if stream_sources.get(source, {}).get("kind") == "file"]
                if paths:
                    execution_events.append({"kind": "read", "position": position, "paths": paths})

    def record_file(node, target, sources, forwards, replaces=True, *, position=None, command_effect=False):
        if file_commands is not None:
            identifiers = sorted((identifier for identifier in sources if identifier != "external"), key=repr)
            for identifier in identifiers or [None]:
                file_commands.append({"target": target, "body": identifier,
                                      "argv": node["argv"], "source": node["source"],
                                      "position": node["index"] if position is None else position,
                                      "owner_position": node["redirection_owner"] if position is not None else node["index"], "command_effect": command_effect,
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

    def flow(nodes, inherited, *, literal_stdin=False):
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
                inputs = flow(input_source[1], inherited, literal_stdin=literal_stdin)
            if node["children"] is None and node["language"] == "shell" and literal_source is not None and len(inputs) == 1:
                child = unwrapped_command(node["argv"], unwrap_code=False)
                name = ("." if child[0] == "." else Path(child[0]).name) if child else None
                fresh = name in {"bash", "sh", "zsh", "dash", "ksh"}
                if fresh and shell_c_body(child) is None or name in {"source", "."}:
                    body = literal_source(next(iter(inputs)))
                    if body is not None:
                        context = name if fresh else node["shell_context"]
                        parsed = embedded(body, context, True)
                        if parsed is not None:
                            scope, begin, stop, children, complete = parsed
                            node.update(children=children, inline_body=(begin, stop, scope), body_scope=scope,
                                        fresh_shell=fresh, child_shell=context, body_complete=complete, stdin_body=True)
                            echo_scopes(children, ("stdin", source, node["index"]) if fresh else node["echo_scope"])
            if execution_events is not None:
                execution_events.append({"kind": "command", "position": node["index"],
                                         "argv": node["argv"], "inline_body": node["inline_body"] is not None or not node["body_complete"],
                                         "source": node["source"],
                                         "literal_stdin": literal_stdin,
                                         "command_effect": True})
                execution_events.extend({"kind": "input_expansion", "position": identifier,
                                         "owner_position": node["redirection_owner"], "source": identifier}
                                        for identifier in node["heredocs"])
            if node["children"] is not None:
                if execution_events is not None and node["inline_body"] is not None:
                    execution_events.append({"kind": "read", "position": node["index"], "paths": [], "command_effect": True})
                # A stdin program is consumed by its interpreter; its children
                # cannot replay the same complete input as a new program.
                produced = flow(node["children"], set() if node["stdin_body"] else inputs,
                                literal_stdin=literal_stdin or node["stdin_body"])
                forwards = bool(produced)
            else:
                for position in node["input_positions"] if node["input_positions"] is not None else (node["index"],):
                    assign_language(inputs, node["language"], position)
                child = stream_argv(node["argv"])
                if execution_events is not None:
                    execution_events.append({"kind": "read", "position": node["index"], "paths": execution_targets(node["argv"]), "command_effect": True})
                    execution_events.extend({"kind": "write", "position": node["index"], "command_effect": True,
                                             "contents": (), "replaces": False, **mutation}
                                            for mutation in source_mutation_paths(node["argv"]))
                substitution_outputs = {}
                for placeholder, (direction, commands) in node["substitutions"].items():
                    if direction == "<":
                        substitution_outputs[placeholder] = flow(commands, inherited, literal_stdin=literal_stdin)
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
                        heredoc_languages(list(lexer), lexer._raw_source, stream_sources=stream_sources,
                                          output_sources=substitution_sources)
                        assign_language(substitution_sources, "shell", node["index"])
                if child and Path(child[0]).name == "tee" and forwards:
                    options = True
                    for word in child[1:]:
                        if options and word == "--":
                            options = False
                        elif not options or not word.startswith("-") or word == "-":
                            if word in node["substitutions"] and node["substitutions"][word][0] == ">":
                                flow(node["substitutions"][word][1], inputs, literal_stdin=literal_stdin)
                            else:
                                record_file(node, word, inputs, True, not any(flag in {"-a", "--append"} for flag in child[1:]), command_effect=True)
            if isinstance(descriptors[1], tuple) and descriptors[1][0] == "file":
                record_file(node, descriptors[1][1], produced, forwards, descriptors[1][2], position=descriptors[1][3])
            elif isinstance(descriptors[1], tuple) and descriptors[1][0] == "process-output":
                flow(descriptors[1][1], produced, literal_stdin=literal_stdin)
            outgoing = produced if descriptors[1] == "pipe-out" else set()
            previous = node["separator"]
            if previous in {"|", "|&"}:
                pipe = outgoing
            else:
                output = append_streams(output, outgoing)
        return output

    def process_subtrees(node):
        """Visit each parsed process body once under its caller/position scope."""
        seen = set()
        bodies = [commands for _, commands in node["substitutions"].values()]
        bodies.extend(value[1] for value in node["descriptors"].values()
                      if isinstance(value, tuple) and value[0] in {"process-input", "process-output"})
        for commands in bodies:
            if commands:
                scope = ("process", node["index"], commands[0]["index"])
                if scope not in seen:
                    seen.add(scope)
                    yield scope, commands

    nodes = parse(0, root_stop, Path(source_shell).name if isinstance(source_shell, str) and source_shell else None)
    if success_facts is not None:
        def hash_changes(action):
            def path_value(value):
                return re.sub(r"^\$(?:PWD|\{PWD\})/", "./", value)
            if action and (assignment := re.fullmatch(r"commands\[([^]\s]+)\]=(.*)", action[0])):
                return (("set", (assignment[1],), path_value(assignment[2]), frozenset({"zsh"})),)
            if action[:1] != ["hash"]:
                return ()
            cursor, flags, path = 1, set(), None
            while cursor < len(action) and action[cursor].startswith("-"):
                word = action[cursor]
                cursor += 1
                if word == "--":
                    break
                for offset, flag in enumerate(word[1:], 1):
                    if flag not in "dlprt":
                        return ()
                    flags.add(flag)
                    if flag == "p":
                        path = word[offset + 1:] or (action[cursor] if cursor < len(action) else None)
                        cursor += not word[offset + 1:] and cursor < len(action)
                        break
            operands = action[cursor:]
            if (path is not None or flags & {"d", "t"}) and not operands:
                return ()
            dialects = frozenset({"bash"}) if flags & {"d", "p"} or "r" in flags and operands else frozenset({"bash", "zsh"})
            changes = [("reset", (), None, dialects)] if "r" in flags else []
            if "t" in flags or "l" in flags:
                return tuple(changes)
            names = [word for word in operands if "/" not in word and "=" not in word]
            if path is not None:
                changes.append(("set", tuple(names), path_value(path), frozenset({"bash"})))
            elif "d" in flags:
                changes.append(("forget", tuple(names), None, frozenset({"bash"})))
            else:
                for word in operands:
                    name, equal, value = word.partition("=")
                    if equal and name and value:
                        changes.append(("set", (name,), path_value(value), frozenset({"zsh"})))
                if names:
                    changes.append(("forget", tuple(names), None, frozenset({"bash"})))
            return tuple(changes)

        def implied_success(nodes, inherited=frozenset(), parent=None):
            implied, previous, list_success, list_status = set(inherited), "", set(inherited), None
            for node in nodes:
                entry = set(implied)
                redirections = {value[3] for value in node["descriptors"].values()
                                if isinstance(value, tuple) and value[0] == "file"}
                child_parent = node["body_scope"] if node["body_scope"] is not None else node["index"]
                child_success = (implied_success(node["children"], entry | redirections, child_parent)
                                 if node["children"] is not None else set())
                for scope, commands in process_subtrees(node):
                    implied_success(commands, entry, scope)
                    # A process substitution copies shell state, but its
                    # asynchronous completion is not its caller's success.
                    success_facts[scope] = {"entry": set(entry), "success": set(), "children": set(),
                                            "parent": node["index"], "subshell": True,
                                            "fresh_shell": False, "asynchronous": True,
                                            "process_substitution": True}
                if node["body_scope"] is not None:
                    if not node["body_complete"]:
                        child_success = set()
                    success_facts[child_parent] = {"entry": entry, "success": set(child_success),
                        "children": child_success, "parent": node["index"], "stop": node["inline_body"][1],
                        "subshell": node["fresh_shell"], "fresh_shell": node["fresh_shell"], "shell": node["child_shell"]}
                    child_success = {child_parent}
                child_success.update(redirections)
                words = node["argv"]
                while words and words[0] in {"if", "then", "else", "elif", "do", "while", "until"}:
                    words = words[1:]
                positive = set() if words[:1] == ["!"] or not node["body_complete"] else {node["index"]}
                action, builtin_action, asynchronous, dispatch = shell_command_action(words)
                opaque_emulate = bool(action[:1] == ["emulate"] and list(executor_commands(action)))
                if asynchronous:
                    positive = set()
                terminates = ("exit" if action[:1] in (["exit"], ["logout"], ["bye"]) else
                              "return" if action[:1] == ["return"] else None)
                if action[:1] == ["exec"] and unwrapped_command(action):
                    terminates = "exit"
                if terminates and action[0] != "exec" and len(action) > 1 and re.fullmatch(r"[+-]?\d+", action[1]) and int(action[1]) % 256:
                    terminates = None  # This early result cannot be success.
                trap = action[1:] if action[:1] == ["trap"] else []
                explicit_action = trap[:1] == ["--"]
                if explicit_action:
                    trap = trap[1:]
                installation = (len(trap) >= 2 and trap[0] not in {"-", ""}
                                and (explicit_action or not trap[0].startswith("-")))
                opaque_context = bool(action[:1] in (["eval"], ["source"], ["."]) and len(action) > 1
                                      or opaque_emulate)
                flags, operands = set(), list(action[1:])
                while operands and operands[0].startswith("-"):
                    flag = operands.pop(0)
                    if flag == "--":
                        break
                    flags.update(flag[1:])
                removes = (operands if (action[:1] == ["unset"] and ("f" in flags or not flags)
                           or action[:1] == ["unfunction"]
                           or action[:1] in (["unhash"], ["disable"]) and "f" in flags) else [])
                alias_installation = bool(action[:1] == ["alias"] and any("=" in word for word in action[1:]))
                opaque_context &= node["inline_body"] is None and node["body_complete"]
                installation |= opaque_context or alias_installation
                signals = {signal.upper() for signal in trap[1:]}
                may_skip = opaque_context or alias_installation or installation and "DEBUG" in signals
                result = shell_literal_status(node["argv"]) if node["body_complete"] else False
                if previous == "||":
                    if list_status is False:
                        list_success = entry | positive
                    elif list_status is not True and result is not False:
                        list_success.intersection_update(entry | positive)
                    list_status = (True if list_status is True or result is True else
                                   False if list_status is False and result is False else None)
                elif previous == "&&":
                    if list_status is not False:
                        list_success.update(positive)
                    list_status = (False if list_status is False or result is False else
                                   True if list_status is True and result is True else None)
                else:
                    list_success, list_status = entry | positive, result
                builtin_change = None
                if action[:1] in (["enable"], ["disable"]):
                    dialects = set()
                    if action[0] == "enable" and flags <= {"n"}:
                        dialects.add("bash")
                    if flags <= {"m"}:
                        dialects.add("zsh")
                    if dialects:
                        builtin_change = (tuple(operands), action[0] == "enable" and "n" not in flags,
                                          frozenset(dialects), "m" in flags)
                success_facts[node["index"]] = {"entry": entry, "success": set(list_success),
                                                "children": child_success, "parent": parent, "stop": node["stop"],
                                                "subshell": node["child_scope"] == "(" or asynchronous, "asynchronous": asynchronous,
                                                "terminates": terminates, "overrides": installation, "builtin_action": builtin_action,
                                                "action": action, "hash_changes": hash_changes(action), "builtin_change": builtin_change, "literal_status": result,
                                                "dispatch": dispatch, "redirections": redirections, "inline_body": node["inline_body"], "opaque_context": opaque_context,
                                                "exports": (tuple(operands), "n" not in flags) if action[:1] == ["export"] and "f" in flags else (tuple(operands), True) if action[:1] in (["declare"], ["typeset"]) and {"f", "x"} <= flags else None, "removes": removes, "removal_patterns": "m" in flags, "definition": node["definition"], "pipeline": previous in {"|", "|&"}, "command": action[0] if action else None, "may_skip": may_skip,
                                                "inherit_override": opaque_context or alias_installation or any(signal not in {"ERR", "ZERR"} for signal in signals)}
                previous = node["separator"]
                implied = set(list_success) if previous == "&&" else set(inherited)
            list_start = 0
            for offset, node in enumerate(nodes):
                if node["separator"] == "&":
                    for member in nodes[list_start:offset + 1]:
                        success_facts[member["index"]]["process_scope"] = node["index"]
                    success_facts[node["index"]]["subshell"] = True
                    success_facts[node["index"]]["asynchronous"] = True
                if node["separator"] in {";", "\n", "&"}:
                    list_start = offset + 1
                if node["separator"] in {"|", "|&"}:
                    success_facts[node["index"]]["subshell"] = True
            return list_success if nodes and previous not in {"&", "|", "|&"} else set(inherited)
        def redirection_scopes(nodes):
            for node in nodes:
                opening = node["redirection_owner"]
                if opening != node["index"]:
                    # Prefix grammar can put the opening token inside the
                    # existing command node rather than at its first token.
                    opening = max(position for position, facts in success_facts.items()
                                  if isinstance(position, int) and position <= opening < facts["stop"])
                    node["redirection_owner"] = opening
                    redirections = success_facts[node["index"]]["redirections"]
                    success_facts[opening]["redirections"].update(redirections)
                    success_facts[opening]["children"].update(redirections)
                    for position, facts in success_facts.items():
                        if isinstance(position, int) and opening <= position < node["index"]:
                            facts["entry"].update(redirections)
                if node["children"] is not None:
                    redirection_scopes(node["children"])
                for _, commands in process_subtrees(node):
                    redirection_scopes(commands)
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
                child_scope = ("child", source, node["index"]) if node["fresh_shell"] else scope
                following = echo_scopes(node["children"], child_scope)
                if not node["fresh_shell"] and tokens[node["index"]] != "(":
                    scope = following
            for process_scope, commands in process_subtrees(node):
                echo_scopes(commands, process_scope)
        return scope
    echo_scopes(nodes, ("native", source))
    produced = flow(nodes, {"external"})
    if success_facts is not None:
        success_facts["end"] = implied_success(nodes)
        redirection_scopes(nodes)
        success_facts["root_stop"] = root_stop
        success_facts["root_shell"] = Path(source_shell).name if isinstance(source_shell, str) and source_shell else None
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


def shell_source(text, *, incremental=True):
    """Retain complete input units and their literal heredoc bodies."""
    # Quoted cat substitutions are a common CLI body argument. Their literal
    # contents must never participate in either shell lexing or '&' detection.
    substitution = re.compile(r"\$\(cat[ \t]+<<(-?)(['\"])([\w-]+)\2[ \t]*\n([\s\S]*?)\n[\t]*\3\r?\n\s*\)")
    text = substitution.sub("inert-heredoc-body", text)
    code, pending, header = [], [], ""
    bodies, unit = [], []
    checkpoint, malformed = (0, 0), False
    for line in text.splitlines(keepends=True):
        if pending:
            delimiter, tabs, expands, body = pending[0]
            if (line.lstrip("\t") if tabs else line).rstrip("\r\n") == delimiter:
                bodies.append(("".join(body), expands))
                pending.pop(0)
                if not pending and compound_bounds(unit, completion=True)[1]:
                    checkpoint, unit = (len(code), len(bodies)), []
            else:
                body.append(line)
            continue
        header += line
        try:
            lexer = shell_lexer(header, posix=False)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            continue  # A quoted word may span several physical lines.
        if len(line.rstrip("\r\n")) - len(line.rstrip("\r\n").rstrip("\\")) & 1:
            continue  # Complete an escaped newline before reading delimiters.
        for index, token in enumerate(tokens):
            if token != "<<":
                continue
            if index + 1 == len(tokens):
                malformed = True
                break
            raw = tokens[index + 1]
            tabs = raw.startswith("-")
            delimiter = GroupShellLexer.words(raw[1:] if tabs else raw)
            if len(delimiter) != 1:
                malformed = True
                break
            pending.append((delimiter[0], tabs, not any(c in raw for c in "'\"\\"), []))
        if malformed:
            break
        code.append(header)
        header = ""
        unit.extend(tokens)
        unit.append("\n")
        if not pending and compound_bounds(unit, completion=True)[1]:
            checkpoint, unit = (len(code), len(bodies)), []
    # Both supported shells accept EOF as the end of a pending heredoc;
    # Bash warns, but still executes the completed input unit.
    if pending and not malformed and not header:
        bodies.extend(("".join(body), expands) for _, _, expands, body in pending)
        pending = []
        if compound_bounds(unit, completion=True)[1]:
            checkpoint, unit = (len(code), len(bodies)), []
    complete = not (malformed or pending or header or unit)
    if not complete:
        if not incremental:
            raise ValueError("unparsed shell input unit")
        code, bodies = code[:checkpoint[0]], bodies[:checkpoint[1]]
    code = "".join(code)
    lexer = shell_lexer(code, posix=False)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    tokens = list(lexer)
    identifiers = [index for index, token in enumerate(tokens) if token == "<<"]
    if len(identifiers) != len(bodies):
        raise ValueError("unparsed heredoc correspondence")
    return code, tokens, dict(zip(identifiers, bodies)), complete


def shell_command_action(words):
    """Normalize same-process shell prefixes to a fixpoint."""
    action, path_assignment = list(words), False
    while action:
        word = action[0]
        if word == "!" or re.match(r"[A-Za-z_]\w*\+?=", word):
            path_assignment |= bool(re.match(r"PATH\+?=", word))
            action = action[1:]
        elif word in {"time", "noglob", "nocorrect"}:
            action = action[1:]
            if word == "time" and action[:1] == ["-p"]:
                action = action[1:]
        else:
            break
    asynchronous = action[:1] == ["coproc"]
    if asynchronous:
        action = action[1:]
    builtin_action = action[:1] in (["builtin"], ["command"])
    dispatch = {"builtin_only": False, "standard_path": False, "path_assignment": path_assignment, "external_command": False}
    while action[:1] in (["builtin"], ["command"]):
        wrapper, action = action[0], action[1:]
        dispatch["builtin_only"], dispatch["standard_path"] = wrapper == "builtin", False
        dispatch["external_command"] = wrapper == "command"
        while action and action[0].startswith("-"):
            option = action[0]
            if option == "--":
                action = action[1:]
                break
            if wrapper == "command" and len(option) > 1 and set(option[1:]) <= {"p", "v", "V"}:
                dispatch["standard_path"] |= "p" in option[1:]
                action = [] if set(option[1:]) & {"v", "V"} else action[1:]
            else:
                break
    return action, builtin_action, asynchronous, dispatch


def shell_word_literal(raw):
    """Whether decoding this word leaves no outer-shell expansion unresolved."""
    quote, escaped, cursor = None, False, 0
    while cursor < len(raw):
        char = raw[cursor]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote == "'":
            if char == "'":
                quote = None
        elif quote is None and raw.startswith("$'", cursor):
            cursor, _ = GroupShellLexer.protected_word(raw, cursor)
            continue
        elif char in "$`":
            return False
        elif char in "'\"":
            quote = None if quote == char else char if quote is None else quote
        cursor += 1
    return True


def shell_code(text, *, writes=None, reads=None, replacements=None, effects=None, commands=None, source_shell=None):
    """Extract bodies first, then resolve streams across the complete command."""
    code, tokens, source_bodies, complete = shell_source(text, incremental=not isinstance(source_shell, str) or Path(source_shell).name != "zsh")
    root_stop = len(tokens)
    file_commands, inline_inputs, stream_sources = [], {}, {}
    execution_events, success_facts = [], {}
    contents, owns, expansion_owners = {}, False, set()
    def literal_source(identifier):
        if identifier in contents:
            return contents[identifier]
        if identifier in source_bodies:
            content, expands = source_bodies[identifier]
            if expands:
                if any(executable_content(expansion) or opaque_ownership("exec_command", {"cmd": expansion})
                       for expansion in shell_expansions(content, heredoc=True)):
                    expansion_owners.add(identifier)
                content = re.sub(r"\\([\\$`\n])", lambda match: "" if match[1] == "\n" else match[1], content)
        elif identifier in inline_inputs:
            raw, content = inline_inputs[identifier]
            if any(executable_content(expansion) or opaque_ownership("exec_command", {"cmd": expansion})
                   for expansion in shell_expansions(raw)):
                expansion_owners.add(identifier)
        else:
            stream = stream_sources.get(identifier, {})
            if stream.get("kind") != "literal":
                return None
            content = stream["content"]
        contents[identifier] = content
        return content

    languages = heredoc_languages(tokens, code, file_commands=file_commands,
                                  inline_inputs=inline_inputs, stream_sources=stream_sources,
                                  execution_events=execution_events, success_facts=success_facts, source_bodies=source_bodies,
                                  source_shell=source_shell, literal_source=literal_source)
    if not complete:
        success_facts["end"] = set()
    for identifier in (*source_bodies, *inline_inputs):
        literal_source(identifier)
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
                                         "owner_position": command["owner_position"], "command_effect": command["command_effect"],
                                         "path": command["target"], "contents": variants,
                                         "replaces": command["replaces"]})
            if replacements is not None and command["replaces"]:
                replacements.add(command["target"])
    if execution_events is not None:
        grouped = {}
        for event in execution_events:
            if event["kind"] == "write":
                # Keep shell redirections separate from effects of the
                # resolved executable; a hash override changes only the latter.
                key = (event["position"], event["path"], bool(event.get("command_effect")))
                if key in grouped:
                    grouped[key]["contents"] += tuple(event["contents"])
                else:
                    grouped[key] = dict(event, contents=tuple(event["contents"]))
        ordered = sorted([*grouped.values(), *(event for event in execution_events if event["kind"] != "write")],
                         # Redirections open before the command mutates its operands.
                         key=lambda event: (event["position"], event["kind"] != "write",
                                            bool(event.get("command_effect"))))
        reached = shell_effect_order(tokens, ordered, success_facts=success_facts)
        owns |= any(ownership_command(unwrapped_command(event["argv"]))
                    or opaque_ownership("exec_command", {"cmd": event["source"]})
                    for event in reached if event["kind"] == "command" and event.get("literal_stdin")
                    and event["argv"] and not event["inline_body"])
        # Embedded commands keep their own raw quotes. Their decoded argv no
        # longer says which substitutions execute in that reached command.
        owns |= any(executable_content(expansion) or opaque_ownership("exec_command", {"cmd": expansion})
                    for event in reached if event["kind"] == "command" and event["argv"] and not event["inline_body"]
                    for expansion in shell_expansions(event["source"]))
        if effects is not None:
            effects.extend(event for event in reached if event["kind"] in {"write", "read"})
        if commands is not None:
            for event in reached:
                if event["kind"] != "command" or not event["argv"] or event["inline_body"]:
                    continue
                argv = event["argv"]
                detached = event.get("detached")
                if event.get("process_substitution"):
                    # A completed parent does not complete an owning process
                    # body. A data producer has no such ownership obligation.
                    detached |= (ownership_command(unwrapped_command(argv))
                                 or opaque_ownership("exec_command", {"cmd": event["source"]}))
                commands.append(["coproc", *argv] if detached and argv[:1] != ["coproc"] else argv)
        owns |= any(event["source"] in expansion_owners for event in reached if event["kind"] == "input_expansion")
        reached_inputs = {(event["source"], event["language"]) for event in reached if event["kind"] == "stdin"}
        if reads is not None:
            for identifier, language in reached_inputs:
                reads.update(file_parts(identifier))
    for identifier, language in reached_inputs:
        if language:
            variants = variants_of(identifier)
            if variants is not None:
                variants = (variants,) if isinstance(variants, UnresolvedStream) else variants
                owns |= any(stdin_ownership(content, language) for content in variants)
    if owns:
        raise ExecutableOwnership("executable stdin")
    # Here-string words are stdin data. Remove them from later argv inspection.
    masked, cursor = [], 0
    for index, token in enumerate(tokens[:root_stop]):
        start = code.find(token, cursor)
        if start < 0:
            continue
        cursor = start + len(token)
        if index - 1 in inline_inputs:
            masked.append((start, cursor))
    for start, end in reversed(masked):
        code = code[:start] + "'inert-stdin-body'" + code[end:]
    return code


def shell_effect_order(tokens, events, *, success_facts=None):
    """Replay repeated bodies and defer function effects until a real call.

    Two iterations suffice for literal file writes: the second read observes
    the first iteration's final replacement, rather than any intermediate write.
    Straight-line effects keep their original command order.
    """
    bounds, definitions, by_position = compound_bounds(tokens), {}, {}
    guards = [True] * len(tokens)
    success_facts = success_facts or {}
    end_success = success_facts.get("end", set())

    def pattern_matches(raw, word):
        # Preserve shell quoting: quoted/escaped glob characters are literals.
        # Expansions and complex unquoted patterns cannot prove an arm definite.
        regex, quote, cursor = [], None, 0
        while cursor < len(raw):
            char = raw[cursor]
            if char == "\\" and quote != "'":
                if cursor + 1 == len(raw):
                    return None
                if quote == '"' and raw[cursor + 1] not in '$`"\\\n':
                    regex.append(re.escape(char))
                else:
                    cursor += 1
                    if raw[cursor] != "\n":
                        regex.append(re.escape(raw[cursor]))
            elif quote and char == quote:
                quote = None
            elif quote is None and char in "'\"":
                quote = char
            elif char in "$`" and quote != "'" or quote is None and char in "[]()":
                return None
            elif quote is None and char in "*?":
                regex.append(".*" if char == "*" else ".")
            else:
                regex.append(re.escape(char))
            cursor += 1
        return re.fullmatch("".join(regex), word, flags=re.DOTALL) is not None if quote is None else None

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
        result = success_facts.get(begin, {}).get("literal_status", shell_literal_status(tokens[begin:end]))
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
        if tokens[start] == "case":
            enclosing = guards[start:stop]
            guard(start, stop, None)
            if start + 2 < stop and tokens[start + 2] == "in":
                word = GroupShellLexer.words(tokens[start + 1])
                known = len(word) == 1 and not re.search(r"[$`]", tokens[start + 1])
                arm, first, selected, uncertain_prior = start + 3, True, False, False
                while arm < stop:
                    close = next((at for at in range(arm, stop) if tokens[at] == ")"), None)
                    if close is None:
                        break
                    finish = next((at for at in range(close + 1, stop) if tokens[at] in {";;", ";&", ";;&"}), stop)
                    matches = [pattern_matches(token, word[0]) for token in tokens[arm:close] if token != "|"] if known else [None]
                    possible = True if True in matches else None if None in matches else False
                    condition = False if selected or possible is False else True if possible is True and not uncertain_prior else None
                    for at in range(start if first else arm, finish):
                        parent = enclosing[at - start]
                        guards[at] = False if condition is False or parent is False else parent if condition is True and enclosing[0] is True else None
                    if possible is True:
                        selected = True
                    elif possible is None:
                        uncertain_prior = True
                    if finish < stop and tokens[finish] != ";;":
                        break  # Fallthrough remains a possible conditional arm.
                    first, arm = False, finish + 1
            continue
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
            condition = shell_literal_status(tokens[start + 1:branch])
            containing = [position for position, facts in success_facts.items() if isinstance(position, int)
                          and position <= start < facts["stop"]]
            parent = success_facts[max(containing)]["parent"] if containing else None
            condition_positions = [position for position, facts in success_facts.items() if isinstance(position, int)
                                   and position < branch and facts["stop"] > start and facts["parent"] == parent]
            if condition_positions:
                condition_success = success_facts[max(condition_positions)]["success"]
                for position in success_facts:
                    if isinstance(position, int) and branch <= position < (other if other is not None else stop):
                        success_facts[position]["entry"].update(condition_success)
            guard(branch, other if other is not None else stop, condition)
            if other is not None:
                inverse = not condition if condition is not None and tokens[other] == "else" else None
                guard(other, stop, inverse)
    definitions = {position: facts["definition"] for position, facts in success_facts.items()
                   if isinstance(position, int) and facts.get("definition") is not None}
    for event in events:
        condition = guards[event["position"]]
        if condition is not False:
            if event["kind"] == "write":
                event = dict(event, _replace_success=event["replaces"])
            by_position.setdefault(event.get("owner_position", event["position"]), []).append(event)
    functions, hashes, builtin_states = {}, {}, {}
    # Core defaults shared by supported Bash3.2/5.3 and zsh shells.
    # Version/build/shell-dependent names retain external possibilities.
    # https://github.com/zsh-users/zsh/blob/zsh-5.9/Src/builtin.c
    # https://www.gnu.org/software/bash/manual/html_node/Optional-Features.html
    bash_builtin_defaults = dict.fromkeys(""". : [ break builtin cd command continue
        declare echo enable eval exec exit export false getopts hash kill let local
        logout printf pwd read readonly return set shift source test times
        trap true type typeset umask unset wait""".split(), frozenset({True}))
    bash_builtin_defaults.update(dict.fromkeys("""alias bg bind caller compgen complete
        compopt dirs disown fc fg help history jobs mapfile popd pushd readarray
        shopt suspend ulimit unalias""".split(), frozenset({True, False})))

    builtin_defaults = {name: {"bash": values, "zsh": values}
                        for name, values in bash_builtin_defaults.items()}
    for name in "alias bg dirs disown fc fg history jobs popd pushd suspend unalias".split():
        builtin_defaults[name]["zsh"] = frozenset({True})
    builtin_defaults["shopt"] = {"bash": frozenset({True}), "zsh": frozenset({False})}
    builtin_defaults["disable"] = {"bash": frozenset({False}), "zsh": frozenset({True})}

    def builtin_default(name, shell_context):
        dialects = {shell_context} if shell_context in {"bash", "zsh"} else {"bash", "zsh"}
        return frozenset((value, dialect) for dialect in dialects
                         for value in builtin_defaults.get(name, {}).get(dialect, frozenset({False})))

    success_links = {position: facts["children"] for position, facts in success_facts.items()
                     if isinstance(position, int) and facts.get("children")}
    success_links["end"] = end_success
    alternatives, function_calls, hooked, entered = {}, set(), set(), set()
    uncertain_commands = {}
    automatic_traps = {"TRAPEXIT", "TRAPERR", "TRAPZERR", "TRAPDEBUG"}

    def expanded_success(positions, active=frozenset()):
        result = set()
        for position in positions:
            if position in active:
                continue
            nested = expanded_success(success_links.get(position, ()), active | {position})
            for endpoint in alternatives.get(position, ()):
                nested.intersection_update(expanded_success(endpoint, active | {position}))
            result.update({position, *nested})
        return result

    def parsed_owner(position):
        if position in success_facts:
            return position
        return max((start for start, facts in success_facts.items() if isinstance(start, int)
                    and start <= position < facts["stop"]), default=None)

    def live_scopes(position, prefix, boundary):
        owner = parsed_owner(position)
        parent = success_facts.get(owner, {}).get("parent")
        groups = []
        while parent is not None and parent != boundary:
            groups.append(parent)
            parent = success_facts.get(parent, {}).get("parent")
        scopes = list(prefix)
        for group in reversed(groups):
            process = success_facts.get(group, {}).get("process_scope")
            if process is not None and process not in scopes:
                scopes.append(process)
            scopes.append(group)
        process = success_facts.get(owner, {}).get("process_scope")
        if process is not None and process not in scopes:
            scopes.append(process)
        if success_facts.get(owner, {}).get("subshell") and owner not in scopes:
            scopes.append(owner)
        return tuple(scopes)

    def alternate_endpoint(scopes, implied, *, termination=None, inherit=True):
        process = next(index for index in range(len(scopes) - 1, -1, -1)
                       if scopes[index] == "end" or success_facts.get(scopes[index], {}).get("subshell"))
        if termination == "exit":
            targets = [scopes[process]]
        elif termination == "return":
            target = next((scope for scope in reversed(scopes[process:])
                           if scope in function_calls or scope != "end" and success_facts.get(scope, {}).get("subshell")), None)
            # Zsh also returns from the noninteractive top-level script.
            targets = [scopes[process]] if target is None else [target]
        else:
            # A reached handler may successfully end its surrounding context.
            # A subshell installation cannot affect its parent's endpoint.
            targets = scopes[process:]
            if inherit:
                hooked.add(scopes[process])
        for target in targets:
            alternatives.setdefault(target, []).append(frozenset(implied))

    def visible_binding(name, scopes, table=None, default=None):
        table = functions if table is None else table
        default = ([], False) if default is None else default
        exported_only = False
        for scope in reversed(scopes):
            if scope != "end" and not success_facts.get(scope, {}).get("subshell"):
                continue
            if (name, scope) in table:
                value = table[name, scope]
                if exported_only:
                    return ([binding for binding in value[0] if binding[3]], False)
                return value
            if scope != "end" and success_facts.get(scope, {}).get("fresh_shell"):
                if table is not functions:
                    return default
                exported_only = True
        return default

    def walk(begin, end, active=(), in_loop=False, definite=True, inherited=frozenset(),
             scope_entry=frozenset(), scope_prefix=("end",), boundary=None, loop_header=False, shell_context=None):
        index = begin
        while index < end:
            if guards[index] is False:
                index = bounds.get(index, index) + 1 if tokens[index] != "case" else index + 1
                continue
            implied = inherited | (success_facts.get(index, {}).get("entry", set()) - scope_entry)
            scopes = live_scopes(index, scope_prefix, boundary)
            for at, scope in enumerate(scopes):
                if scope not in entered:
                    entered.add(scope)
                    if any(parent in hooked for parent in scopes[:at]):
                        alternatives.setdefault(scope, []).append(frozenset(implied))
            process = next(scope for scope in reversed(scopes)
                           if scope == "end" or success_facts.get(scope, {}).get("subshell"))
            certain_scope = not any(scope != "end" and success_facts.get(scope, {}).get("pipeline") for scope in scopes)
            certain_scope &= not success_facts.get(index, {}).get("pipeline", False)
            if index in definitions:
                name, start, stop = definitions[index]
                if guards[index] is not False:
                    old, certain = visible_binding(name, scopes)
                    # Bash replaces a function value without clearing export attributes.
                    binding = (parsed_owner(index), start, stop, any(binding[3] for binding in old))
                    functions[name, process] = (([binding], True) if definite is True and guards[index] is True and certain_scope else
                                                ([*old, binding], certain))
                    if name in automatic_traps:
                        alternate_endpoint(scopes, implied, inherit=name not in {"TRAPERR", "TRAPZERR"})
                        if name == "TRAPDEBUG":
                            uncertain_commands.setdefault(process, []).append(frozenset(implied))
                index = stop
                continue
            token = tokens[index]
            if not loop_header and token in {"for", "select", "while", "until"} and index in bounds:
                stop = bounds[index]
                header_end = next((at for at in range(index + 1, stop) if tokens[at] in {";", "\n"}), stop)
                header = tokens[index + 1:header_end]
                iterations, guaranteed = 2, False
                if token == "for" and len(header) >= 2 and header[1] == "in":
                    items = header[2:]
                    if not any(re.search(r"[$`*?{]", word) for word in items):
                        iterations = min(2, len(items))
                        guaranteed = bool(items)
                if token == "while" and header == ["true"] or token == "until" and header == ["false"]:
                    guaranteed = True
                if token == "while" and header == ["false"] or token == "until" and header == ["true"]:
                    iterations = 0
                for iteration in range(max(1, iterations)):
                    # A while/until condition is a reached command even when
                    # its body never runs. Reuse normal dispatch for that node.
                    if iteration == 0 or token in {"while", "until"}:
                        previous_link = success_links.get(index, set())
                        for event in walk(index, index + 1, active, in_loop=in_loop, definite=definite,
                                          inherited=inherited, scope_entry=scope_entry,
                                          scope_prefix=scope_prefix, boundary=boundary, loop_header=True, shell_context=shell_context):
                            # Successful loop completion does not certify that
                            # a failing condition completed its intrinsic writes.
                            yield dict(event, replaces=False, _replace_success=False) if event["kind"] == "write" and event.get("command_effect") else event
                        success_links[index] = previous_link
                    if not iterations:
                        break
                    transfer = None
                    for event in walk(index + 1, stop, active, in_loop=True,
                                      definite=True if definite is True and guards[index] is True and guaranteed else None,
                                      inherited=implied, scope_entry=success_facts.get(index, {}).get("entry", set()),
                                      scope_prefix=scope_prefix, boundary=boundary, shell_context=shell_context):
                        if event["kind"] in {"break", "continue"}:
                            transfer = event["kind"]
                            break
                        yield event
                    if transfer == "break":
                        break
                index = stop + 1
                continue
            current = by_position.get(index, ())
            # Redirections belong to the shell, including before a function call.
            for event in current:
                if event["kind"] == "input_expansion" or event["kind"] == "write" and not event.get("command_effect"):
                    yield event
            current = tuple(event for event in current if event["kind"] != "input_expansion"
                            and (event["kind"] != "write" or event.get("command_effect")))
            if (in_loop and token in {"break", "continue"}
                    and any(event["kind"] == "read" and event["paths"] == [token] for event in current)):
                if definite is True and guards[index] is True:
                    yield {"kind": token}
                    return
                definite = None
                index += 1
                continue
            facts = success_facts.get(index, {})
            called = facts.get("command") if not facts.get("builtin_action") else None
            possible, certain = visible_binding(called, scopes) if called is not None else ([], False)
            if possible and active.count(called) < 2:
                body_guarantees = []
                function_calls.add(index)
                if process in hooked:
                    alternatives.setdefault(index, []).append(frozenset(implied))
                for definition, start, stop, exported in possible:
                    definition_facts = success_facts.get(definition, {})
                    body_guarantees.append(definition_facts.get("children", set()) - definition_facts.get("entry", set()))
                    yield from walk(start, stop, (*active, called),
                                    definite=True if definite is True and guards[index] is True and certain_scope and certain and len(possible) == 1 else None,
                                    inherited=implied | facts.get("redirections", set()), scope_entry=definition_facts.get("entry", set()),
                                    scope_prefix=(*scopes, index), boundary=definition, shell_context=shell_context)
                success_links[index] = (set.intersection(*body_guarantees) if certain else set()) | facts.get("redirections", set())
            if not certain:
                dispatch = facts.get("dispatch", {})
                exact = definite is True and guards[index] is True and certain_scope and not possible
                command = facts.get("command")
                availability = visible_binding(command, scopes, builtin_states, builtin_default(command, shell_context))
                available = frozenset(value for value, dialect in availability)
                # Optional builds can execute an explicitly requested disabled builtin.
                builtin_possible = True in available or dispatch.get("builtin_only") and command in builtin_defaults
                # zsh command normally forces external dispatch; Bash may
                # retain its builtin. Explicit builtin / command -p stay precise.
                hash_dialects = {dialect for value, dialect in availability
                                 if not value or dispatch.get("external_command") and dialect != "bash"}
                lookup_command = command
                if command == "exec":
                    target = unwrapped_command(facts.get("action", ()), unwrap_code=False)
                    lookup_command = target[0] if target and "/" not in target[0] else None
                    hash_dialects = {shell_context} if shell_context in {"bash", "zsh"} else {"bash", "zsh"}
                if current and dispatch.get("path_assignment") and command is None and exact:
                    for name in {name for name, _ in hashes}:
                        hashes[name, process] = frozenset()
                if current and (builtin_possible or command and command.startswith("commands[")):
                    for operation, names, path, dialects in facts.get("hash_changes", ()):
                        dialects &= {shell_context} if shell_context in {"bash", "zsh"} else {"bash", "zsh"}
                        if not dialects:
                            continue
                        names = {name for name, _ in hashes} if operation == "reset" else names
                        for name in names:
                            previous = visible_binding(name, scopes, hashes, frozenset())
                            changed = frozenset((path, dialect) for dialect in dialects) if operation == "set" else frozenset()
                            exact_dialects = dialects - ({"zsh"} if dispatch.get("external_command") else set())
                            exact_dialects &= {dialect for dialect in dialects
                                               if (True, dialect) in availability and (False, dialect) not in availability}
                            retained = frozenset(binding for binding in previous if binding[1] not in exact_dialects)
                            hashes[name, process] = retained | changed if exact else previous | changed
                    if facts.get("builtin_change"):
                        operands, enabled, dialects, patterns = facts["builtin_change"]
                        for name in builtin_defaults:
                            matches = [pattern_matches(operand, name) if patterns else operand == name for operand in operands]
                            if not any(match is not False for match in matches):
                                continue
                            previous = visible_binding(name, scopes, builtin_states, builtin_default(name, shell_context))
                            if shell_context in {"bash", "zsh"}:
                                dialects = dialects & {shell_context}
                            changed = (frozenset(binding for binding in builtin_default(name, shell_context) if binding[1] in dialects)
                                       if enabled else frozenset((False, dialect) for dialect in dialects))
                            exact_dialects = {dialect for dialect in dialects
                                              if (True, dialect) in availability and (False, dialect) not in availability}
                            if True not in matches:
                                exact_dialects = set()
                            retained = frozenset(binding for binding in previous if binding[1] not in exact_dialects)
                            builtin_states[name, process] = retained | changed if exact else previous | changed
                paths = frozenset()
                if (current and (command == "exec" or not dispatch.get("builtin_only")) and not dispatch.get("standard_path")
                        and not dispatch.get("path_assignment") and hash_dialects):
                    paths = frozenset(path for path, dialect in visible_binding(lookup_command, scopes, hashes, frozenset())
                                      if dialect in hash_dialects)
                    if paths:
                        current = tuple(dict(event, replaces=False, _replace_success=False)
                                        if event["kind"] == "write" and event.get("command_effect") else event for event in current)
                        current = (*current, {"kind": "read", "position": index, "paths": sorted(paths)})
                if current and facts.get("exports"):
                    names, exported = facts["exports"]
                    for name in names:
                        bindings, binding_certain = visible_binding(name, scopes)
                        functions[name, process] = ([(*binding[:3], exported) for binding in bindings], binding_certain)
                if facts.get("inline_body"):
                    begin, stop, body_scope = facts["inline_body"]
                    body_facts = success_facts[body_scope]
                    # A possible hash wrapper can forward code in another shell.
                    child_shell = (None if paths else body_facts.get("shell")) if body_facts.get("fresh_shell") else shell_context
                    for event in walk(begin, stop, active, definite=None if paths else definite,
                                      inherited=implied | facts.get("redirections", set()), scope_entry=facts.get("entry", set()),
                                      scope_prefix=(*scopes, body_scope), boundary=body_scope, shell_context=child_shell):
                        yield (dict(event, replaces=False, _replace_success=False)
                               if paths and event["kind"] == "write" else event)
                elif facts.get("opaque_context") or command and re.search(r"[$`]", command):
                    # Unknown same-context code can call visible functions, but
                    # a parsed literal body is traversed precisely above.
                    for name in {name for name, _ in functions}:
                        bindings, _ = visible_binding(name, scopes)
                        if active.count(name) >= 2:
                            continue
                        for definition, begin, stop, exported in bindings:
                            yield from walk(begin, stop, (*active, name), definite=None,
                                            inherited=implied, scope_entry=success_facts.get(definition, {}).get("entry", set()),
                                            scope_prefix=(*scopes, index), boundary=definition, shell_context=shell_context)
                if current and facts.get("removes"):
                    for name in {name for name, _ in functions}:
                        if any(re.search(r"[$`]", operand) or (pattern_matches(operand, name) is not False
                               if facts.get("removal_patterns") else operand == name)
                               for operand in facts["removes"]):
                            bindings, _ = visible_binding(name, scopes)
                            functions[name, process] = (list(bindings), False)
                if current and facts.get("terminates"):
                    alternate_endpoint(scopes, implied, termination=facts["terminates"])
                if current and facts.get("overrides"):
                    alternate_endpoint(scopes, implied, inherit=facts.get("inherit_override", False))
                    if facts.get("may_skip"):
                        uncertain_commands.setdefault(process, []).append(frozenset(implied))
                observed = set(implied) | facts.get("redirections", set())
                for scope in scopes:
                    for before in uncertain_commands.get(scope, ()):
                        observed.intersection_update(before)
                for event in current:
                    if event["kind"] == "command":
                        asynchronous = [success_facts.get(scope, {}) for scope in scopes
                                        if scope != "end" and success_facts.get(scope, {}).get("asynchronous")]
                        yield dict(event, detached=any(not fact.get("process_substitution") for fact in asynchronous),
                                   process_substitution=any(fact.get("process_substitution") for fact in asynchronous))
                    else:
                        yield dict(event, _implied_success=tuple(observed)) if event["kind"] == "read" else event
            index += 1
    ordered = list(walk(0, success_facts.get("root_stop", len(tokens)), shell_context=success_facts.get("root_shell")))
    native_success = expanded_success({"end"})
    return [dict(event, replaces=event["replaces"] and event["position"] in native_success,
                 partial=event.get("partial", not event["replaces"])) if event["kind"] == "write" else
            dict(event, _implied_success=tuple(expanded_success(event.get("_implied_success", ()))))
            for event in ordered]


def executable_content_cached(value, cache):
    if value not in cache:
        cache[value] = executable_content(value)
    return cache[value]


def _unparsed_command_text(value, *, generated_sources=True, capabilities=False, sloppy=False, writer_cache=None):
    """Keep command expressions and referenced declarations, excluding JS prose.

    This is conservative command discovery, not evaluation or completion proof.
    Parsed literal tool arguments have already been removed by the caller.
    """
    writer_cache = {} if writer_cache is None else writer_cache
    unparsed = list(value)
    for match in composed_shell_fields(value):
        try:
            composed_string(match[1], value)
        except ValueError:
            continue
        unparsed[match.start():match.end()] = " " * (match.end() - match.start())
    value = "".join(unparsed)
    projection_facts = {}
    atoms, code, _, complete = javascript_projection(value, _facts=projection_facts)
    literals = [atom for atom in atoms if not atom[0].lstrip(" \t").startswith(("//", "/*"))]
    generated = list(javascript_generated_sources(value)) if generated_sources else []
    def region(start, delimiter):
        start += len(re.match(r"\s*", value[start:])[0])
        end = re.search(delimiter, code[start:])
        return start, start + end.start() if end else len(code)
    direct_eval_sites = tuple(start for start, _, _, direct, _ in javascript_source_calls(value, code,
        literal_atoms=atoms, templates=projection_facts["templates"]) if direct)
    declarations, bound = javascript_bindings(code, sloppy=sloppy, _direct_eval_sites=direct_eval_sites)
    projections = [javascript_projection(text) for text in generated]
    projected = [code, *(projection[1] for projection in projections)]
    capabilities |= javascript_capabilities(code, value) or any(javascript_capabilities(projection[1], text)
                        for text, projection in zip(generated, projections))
    # Each reached source contributes literal writers once.
    generated_writers = tuple(dict.fromkeys(text for projection in projections
        for text in javascript_source_literals(projection[0])
        if executable_content_cached(text, writer_cache)))
    output_only = javascript_output_only(code, bound, capabilities)
    pending = [] if output_only else [region(m.end(), r"[,};\n]") for m in re.finditer(r"\b(?:cmd|command)\s*:", code)]
    if not output_only:
        pending += [(m.start(1), m.end(1)) for m in re.finditer(r"[{,]\s*(cmd|command)\s*(?=[,}])", code)]
        for match in re.finditer(r"([\"'])(cmd|command)\1\s*:", value):
            key_end = match.start() + len(match[2]) + 2
            if any(atom.start() == match.start() and atom.end() == key_end for atom in atoms):
                pending.append(region(match.end(), r"[,};\n]"))

    seen, pieces, unresolved = set(), [], False
    if not complete:
        # Truncated/invalid native source is never proof that a command ended
        # before its unparsed suffix. Preserve it conservatively.
        pieces.append(value)
    nested_unresolved = False
    for text in generated:
        for match in composed_shell_fields(text):
            try:
                command = composed_string(match[1], text)
            except ValueError:
                continue
            pieces.extend(shlex.join(argv) for argv in shell_commands("exec_command", {"cmd": command})
                          if ownership_command(argv))
        nested, child_unresolved = _unparsed_command_text(text, generated_sources=False,
            capabilities=capabilities, writer_cache=writer_cache)
        pieces.append(nested)
        nested_unresolved |= child_unresolved
    def literal_writers():
        for literal in literals:
            try:
                text = ast.literal_eval(literal[0]) if not literal[0].startswith('`') else literal[0][1:-1]
            except (ValueError, SyntaxError):
                continue
            if isinstance(text, str) and executable_content_cached(text, writer_cache):
                yield text
    local_writers = None
    def writers():
        nonlocal local_writers
        if local_writers is None:
            local_writers = tuple(literal_writers())
        return (*local_writers, *generated_writers)
    while pending:
        start, end = pending.pop()
        if (start, end) in seen:
            continue
        seen.add((start, end))
        expression = value[start:end].strip()
        if expression.startswith(("'", '"', '`')):
            try:
                command = javascript_string(expression)
            except ValueError:
                pass
            else:
                pieces.extend(shlex.join(argv) for argv in shell_commands("exec_command", {"cmd": command})
                              if ownership_command(argv))
                continue
        if expression.startswith('`') and re.fullmatch(JS_STRING, expression) is not None:
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
            if (safe_substitutions or not writers()) and not any(c in command for c in "`$;&|\n"):
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
        unresolved |= any(m[0].startswith("String.raw") for m in literals if start <= m.start() < end)
    if unresolved:
        # Parameters, destructuring and for-of bindings do not have a simple
        # declaration to follow. They must not hide executable cell literals.
        pieces.extend(literal_writers())
    if unresolved or nested_unresolved:
        pieces.extend(generated_writers)
    return " ".join(dict.fromkeys(pieces)), unresolved


def unparsed_command_text(value, *, generated_sources=True):
    return _unparsed_command_text(value, generated_sources=generated_sources)[0]


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
        unparsed = unparsed_command_text(value)
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
    if name == "emulate":
        mode = next((index for index, word in enumerate(argv[1:], 1) if not word.startswith("-")), len(argv))
        # -l queries and -L is incompatible with code execution. Only -R may
        # precede the mode; invocation flags, including clustered -c, follow it.
        if mode < len(argv) and all(re.fullmatch(r"-R+", word) for word in argv[1:mode]):
            body = shell_c_body(["zsh", *argv[mode + 1:]])
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
    quote, escaped, boundary, cursor = None, False, True, 0
    while cursor < len(text):
        char = text[cursor]
        if escaped:
            escaped = False
            if char != "\n":
                boundary = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif not heredoc and quote is None and boundary and char == "#":
            end = text.find("\n", cursor)
            cursor = len(text) if end < 0 else end
            continue
        elif not heredoc and quote is None and text.startswith("$'", cursor):
            cursor, _ = GroupShellLexer.protected_word(text, cursor)
            boundary = False
            continue
        elif (quote != "'" and text.startswith(("$((", "$["), cursor)
              and (protected := GroupShellLexer.protected_word(text, cursor)) is not None):
            end, _ = protected
            bracket = text.startswith("$[", cursor)
            yield from shell_expansions(text[cursor + (2 if bracket else 3):end - (1 if bracket else 2)])
            cursor = end
            boundary = False
            continue
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
                elif ((inner_quote is None and text.startswith("$'", end)
                       or inner_quote != "'" and text.startswith(("$((", "$["), end))
                      and (protected := GroupShellLexer.protected_word(text, end)) is not None):
                    end, _ = protected
                    continue
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
            boundary = False
            continue
        elif (heredoc or quote != "'") and char == "`":
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
            boundary = False
            continue
        elif quote:
            if char == quote:
                quote = None
        elif not heredoc and char in "'\"":
            quote = char
            boundary = False
        else:
            boundary = char.isspace() or char in ";&|<>()"
        cursor += 1


def unwrapped_command(argv, *, unwrap_code=True, stop_at=()):
    """Transparent process wrappers change execution, never operand meaning."""
    argv = list(argv)
    while argv:
        name = Path(argv[0]).name
        if name in stop_at:
            break
        if name == "coproc" and len(argv) > 2 and argv[2] in {"{", "(", "if", "for", "while", "until", "case"}:
            argv = argv[2:]
        elif re.match(r"[A-Za-z_]\w*\+?=", argv[0]) or name in {"if", "elif", "while", "until", "!", "{", "(", "do", "then", "else", "builtin", "noglob", "nocorrect", "coproc"}:
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
            while argv and (argv[0].startswith("-") or re.match(r"[A-Za-z_]\w*\+?=", argv[0])):
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
                elif name == "exec" and re.fullmatch(r"-[cl]*a", flag) and argv:
                    argv.pop(0)
            if name == "timeout" and argv:
                argv = argv[1:]
        elif name == "uv":
            child = uv_run_argv(argv)
            if child is None:
                break
            # uv's '-' is Python stdin even behind env/assignment wrappers.
            # Preserve that execution mode while normalizing the launcher.
            argv = ["python", *child] if child[:1] == ["-"] else child
        elif unwrap_code and name == "emulate":
            bodies = list(executor_commands(argv))
            child = foreground_shell_argv(bodies[0]) if len(bodies) == 1 else None
            if child is None:
                break
            argv = child
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


def python_ownership(content, *, partial=False):
    """Follow Python execution operands through bounded values and lexical scopes."""
    try:
        tree = ast.parse(textwrap.dedent(content))
    except SyntaxError:
        return False
    launches = {"os.system", "os.popen", "subprocess.run", "subprocess.Popen", "subprocess.call",
                "subprocess.check_call", "subprocess.check_output", "exec", "eval"}
    unknown = object()
    computed, overflow = "$computed_operand", "$computed_ownership_operand"
    found = False
    active, invocation_cache, active_iterators = [], {}, set()
    recursion_skips = 0
    class_bindings, expression_values, created_classes = {}, {}, []
    exit_callbacks = []
    patched_attributes, executing_sources, parsed_sources = {}, [], {}
    factored_sources = set()
    started_patches = {}
    mock_types = {"unittest.mock." + name for name in ("Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock")}
    async_calls = {"asyncio.run": "main", "asyncio.create_task": "coro", "asyncio.ensure_future": "coro_or_future",
                   "asyncio.wait_for": "fut", "asyncio.AbstractEventLoop.run_until_complete": "future"}
    loop_factories = {"asyncio.new_event_loop", "asyncio.get_event_loop", "asyncio.get_running_loop"}
    async_consumers = set(async_calls) | {"asyncio.gather"}
    async_names = async_consumers | loop_factories
    exception_types = {"builtins." + name for name, kind in vars(builtins).items()
                       if isinstance(kind, type) and issubclass(kind, BaseException)} | {
                           "subprocess.SubprocessError", "subprocess.CalledProcessError", "subprocess.TimeoutExpired"}
    return_values, yield_values = [], []
    fingerprints = {}
    branch_changes = []
    branch_scopes = []
    class_bases = {}
    method_classes = {}
    attribute_mutations = 0

    class Scope:
        def __init__(self, parent=None, local_names=None, globals_=(), nonlocals=()):
            self.parent, self.local_names, self.state = parent, local_names, {}
            self.declarations = {}
            for name in globals_:
                owner = self
                while owner.parent is not None:
                    owner = owner.parent
                self.declarations[name] = owner
            for name in nonlocals:
                owner = parent
                while owner is not None:
                    if owner.local_names is not None and name in owner.local_names:
                        self.declarations[name] = owner
                        break
                    owner = owner.parent

        def get(self, name, default=None):
            if name in self.declarations:
                return self.declarations[name].get(name, default)
            if name in self.state:
                return self.state[name]
            if self.local_names is not None and name in self.local_names:
                return default
            return self.parent.get(name, default) if self.parent is not None else default

        def owner(self, name):
            if name in self.declarations:
                return self.declarations[name].owner(name)
            if name in self.state or self.local_names is not None and name in self.local_names:
                return self
            return self.parent.owner(name) if self.parent is not None else None

        def __setitem__(self, name, assigned):
            owner = self.declarations.get(name, self)
            if owner is not self:
                observe_scope(owner)
            owner.state[name] = assigned

    class Function:
        def __init__(self, node, scope):
            self.node, self.scope = node, scope
            parameters = [*node.args.posonlyargs, *node.args.args]
            self.defaults = {parameter.arg: value(default, scope) for parameter, default in zip(parameters[-len(node.args.defaults):], node.args.defaults)} if node.args.defaults else {}
            self.defaults.update({parameter.arg: value(default, scope) for parameter, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is not None})
            loaded, nested_loaded, local = set(), set(), {parameter.arg for parameter in (*parameters, *node.args.kwonlyargs)}
            local.update(parameter.arg for parameter in (node.args.vararg, node.args.kwarg) if parameter is not None)
            self.globals, self.nonlocals = set(), set()
            self.generator = False
            def names(item):
                if isinstance(item, (ast.Global, ast.Nonlocal)):
                    (self.globals if isinstance(item, ast.Global) else self.nonlocals).update(item.names)
                    return
                if isinstance(item, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                    for generator in item.generators:
                        names(generator.iter)
                        for condition in generator.ifs:
                            names(condition)
                    if isinstance(item, ast.DictComp):
                        names(item.key)
                        names(item.value)
                    else:
                        names(item.elt)
                    return
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and item is not node:
                    local.add(item.name)
                    nested_loaded.update(child.id for child in ast.walk(item) if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load))
                    return
                if isinstance(item, ast.Lambda) and item is not node:
                    nested_loaded.update(child.id for child in ast.walk(item) if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load))
                    return
                if isinstance(item, (ast.Yield, ast.YieldFrom)):
                    self.generator = True
                if isinstance(item, ast.Name):
                    (loaded if isinstance(item.ctx, ast.Load) else local).add(item.id)
                if isinstance(item, (ast.Import, ast.ImportFrom)):
                    local.update(alias.asname or (alias.name.split('.')[0] if isinstance(item, ast.Import) else alias.name) for alias in item.names)
                for child in ast.iter_child_nodes(item):
                    names(child)
            names(node)
            self.locals = local - self.globals - self.nonlocals
            # Nested bodies/defaults/decorators can read late bindings even when
            # this function binds a same-named local. Keep a safe cache footprint.
            self.free = (loaded - self.locals) | nested_loaded

    class Descriptor:
        def __init__(self, function, kind, setter=(), deleter=()):
            self.function, self.kind = function, kind
            self.setter, self.deleter = setter, deleter

    class Bound:
        def __init__(self, function, receiver):
            self.function, self.receiver = function, receiver

    class Instance:
        def __init__(self, kind):
            self.kind, self.attributes = kind, {}
            self.json_payload = ()

    class Super:
        def __init__(self, kind, receiver):
            self.kind, self.receiver = kind, receiver

    class Partial:
        def __init__(self, functions, arguments, keywords):
            self.functions, self.arguments, self.keywords = functions, arguments, keywords

    class Registry:
        def __init__(self):
            self.handlers = []

    class CompiledCode:
        def __init__(self, source):
            self.source = source

    class PatchContext:
        def __init__(self, target, replacement, inject=False, children=(), attribute_owner=None, create=False):
            self.target, self.replacement, self.previous = target, replacement, []
            self.inject = inject
            self.children = children
            self.attribute_owner = attribute_owner
            self.create = create

    class ExceptionValue:
        def __init__(self, name):
            self.name = name

    class Deferred:
        def __init__(self, callback):
            self.callback = callback

    class RowConcat:
        def __init__(self, left, right):
            self.left, self.right = tuple(left), tuple(right)

    def process_salted(item):
        return isinstance(item, (str, bytes)) or isinstance(item, (tuple, list, dict)) and any(process_salted(child) for child in item)

    class Sequence:
        def __init__(self, rows, unique=False):
            if unique:
                unique_rows = []
                for row in rows:
                    try:
                        row = list(dict.fromkeys(row))
                    except TypeError:
                        row = list(choices(row))
                    unique_rows.append(row)
                rows = unique_rows
            self.state = {"rows": choices(row if isinstance(row, RowConcat) else list(row) for row in rows)}
            self.unique = unique

    class Keywords:
        def __init__(self, items):
            self.state = items

        @property
        def items(self):
            return self.state

    def row_dag_values(rows, leaf, concatenate, alternatives):
        # RowConcat snapshots are immutable and may share entire prior states.
        # Visit each node once without consuming Python's call stack.
        memo = {}
        pending = [(row, False) for row in reversed(rows)]
        while pending:
            row, ready = pending.pop()
            identity = id(row)
            if identity in memo:
                continue
            if isinstance(row, RowConcat):
                if not ready:
                    pending.append((row, True))
                    pending.extend((child, False) for child in reversed((*row.left, *row.right)))
                    continue
                memo[identity] = concatenate(
                    alternatives(memo[id(child)] for child in row.left),
                    alternatives(memo[id(child)] for child in row.right))
            else:
                memo[identity] = leaf(row)
        return tuple(memo[id(row)] for row in rows)

    def sequence_rows(item):
        if not isinstance(item, Sequence):
            return (item,)
        # No callback or mutable operation runs during this fold. Retain each
        # classified row itself so identities cannot be recycled, then discard
        # the cache before the caller can mutate any referenced container.
        row_cache = {}
        def concatenate(left, right):
            if right and all(len(suffix) == 1 for suffix in right):
                return tuple(combinations((tuple(suffix[0] for suffix in right),), initial=left, row_cache=row_cache))
            return choices(([*first, *second] for first in left for second in right), row_cache=row_cache)
        def alternatives(values):
            distinct = {}
            for rows in values:
                for row in rows:
                    distinct.setdefault(id(row), row)
            return choices(distinct.values(), row_cache=row_cache)
        rows = alternatives(row_dag_values(
            item.state["rows"],
            lambda row: (row if isinstance(row, (list, tuple)) else [row],),
            concatenate, alternatives))
        if item.unique:
            # Unique collections do not expose insertion order.
            # Keep shape and a writer witness without enumerating permutations.
            return choices((*rows, *([widen(row)] * len(row) for row in rows
                           if len(row) > 1)))
        return rows

    def sequence_members(item):
        seen = set()
        pending = list(reversed(item.state["rows"]))
        while pending:
            row = pending.pop()
            if id(row) in seen:
                continue
            seen.add(id(row))
            if isinstance(row, RowConcat):
                pending.extend(reversed((*row.left, *row.right)))
            elif isinstance(row, (list, tuple)):
                yield from row
            else:
                yield row

    class Generator:
        def __init__(self, node, scope, arguments=(), keywords=(), operands=None):
            self.node, self.scope = node, scope
            self.arguments, self.keywords, self.operands = arguments, keywords, operands
            self.positions = None
            self.created_active = tuple(active)
            self.state = {"exhausted": (False,), "partial": (False,)}

    def exhaust_generators(assigned):
        for item in assigned:
            if isinstance(item, Generator):
                observe(item.state)
                item.state["exhausted" if len(assigned) == 1 else "partial"] = (True,)

    def same(left, right):
        if isinstance(left, ast.alias) and isinstance(right, ast.alias):
            return left.name == right.name
        return left == right

    def executable_value(item):
        if isinstance(item, Sequence):
            return any(executable_value(row) for row in sequence_rows(item))
        if isinstance(item, (list, tuple)):
            argv = [word if isinstance(word, str) else computed for word in item]
            text = shlex.join(argv)
            return ownership_command(unwrapped_command(argv)) or opaque_ownership("exec_command", {"cmd": text}) or overflow_execution(text)
        return isinstance(item, str) and executable_text_value(item)

    @lru_cache(maxsize=512)
    def executable_text_value(text):
        return executable_content(text) or opaque_ownership("exec_command", {"cmd": text}) or overflow_execution(text)

    def helper_witness(item):
        if isinstance(item, Sequence):
            return None
        if isinstance(item, str):
            commands = list(shell_commands("exec_command", {"cmd": item}))
            if len(commands) != 1:
                return None
            argv = commands[0]
        elif isinstance(item, (list, tuple)) and all(isinstance(word, str) for word in item):
            argv = item
        else:
            return None
        name, action = helper_action(argv)
        if name not in {"gh-plan.py", "gh-pr.py", "gh"}:
            return None
        if not action:
            return name, None
        if name == "gh":
            return None
        parser = helper_parser(name)
        verbs = {verb for option in parser._actions if isinstance(option, argparse._SubParsersAction) for verb in option.choices}
        flags = {flag for option in parser._actions for flag in option.option_strings}
        return (name, action[0]) if action[0] in verbs | flags else None

    def widen(items):
        if all(same(item, items[0]) for item in items):
            return items[0]
        if all(isinstance(item, (list, tuple)) for item in items) and len({len(item) for item in items}) == 1:
            return [widen(list(column)) for column in zip(*items)]
        marker = overflow if any(executable_value(item) for item in items) else computed
        if all(isinstance(item, str) for item in items):
            prefix = ""
            for column in zip(*items):
                if len(set(column)) != 1:
                    break
                prefix += column[0]
            suffix = ""
            for column in zip(*(item[len(prefix):][::-1] for item in items)):
                if len(set(column)) != 1:
                    break
                suffix = column[0] + suffix
            return prefix + marker + suffix
        return marker

    def literal_choice_key(item, seen=()):
        if item is unknown:
            return ("unknown",)
        if type(item) in (str, bytes, int, bool, float, complex, type(None)):
            return ("literal", item) if item == item else None
        if type(item) in (list, tuple) and id(item) not in seen:
            children = tuple(literal_choice_key(child, (*seen, id(item))) for child in item)
            if all(child is not None for child in children):
                return type(item), children
        return None

    def choices(items, *, source_values=True, row_cache=None):
        def classify(kind, item, operation):
            if row_cache is None:
                return operation(item)
            key = kind, id(item), len(factored_sources) if kind == "source" else None
            cached = row_cache.get(key)
            if cached is None or cached[0] is not item:
                cached = row_cache[key] = item, operation(item)
            return cached[1]
        unique, strings, literals = [], set(), set()
        for item in items:
            if isinstance(item, str):
                if item in strings:
                    continue
                strings.add(item)
                unique.append(item)
            else:
                key = classify("literal", item, literal_choice_key)
                if key is not None:
                    if key not in literals:
                        literals.add(key)
                        unique.append(item)
                elif not any(same(item, prior) for prior in unique):
                    unique.append(item)
        if len(unique) <= 8 or any(isinstance(item, (ast.AST, Function, Descriptor, Bound, Instance, Partial, Registry, Deferred, Keywords, Sequence, Generator, CompiledCode, PatchContext, RowConcat)) for item in unique):
            return tuple(unique)
        if source_values:
            groups, other = {}, []
            for item in unique:
                prefix = classify("prefix", item, python_source_prefix)
                if prefix is None:
                    other.append(item)
                else:
                    groups.setdefault(prefix, []).append(item)
            if groups:
                return (*[item for group in groups.values() for item in choices(group, source_values=False, row_cache=row_cache)],
                        *choices(other, row_cache=row_cache))
        # Executable Python fixtures must reach exec/eval as concrete sources;
        # the shell-argv widening marker would bypass their dynamic patches.
        def python_source(item):
            if isinstance(item, (list, tuple)):
                if all(isinstance(word, str) for word in item):
                    child = unwrapped_command(item)
                    if child and re.fullmatch(r"python[0-9.]*", Path(child[0]).name) and "-c" in child:
                        position = child.index("-c") + 1
                        return (position < len(child) and child[position] not in factored_sources
                                and classify("source", child[position], python_source))
                return any(classify("source", child, python_source) for child in item)
            if not isinstance(item, str):
                return False
            embedded = parsed_source(item, "exec")
            return embedded is not None and any(isinstance(node, (ast.Call, ast.Import, ast.ImportFrom)) for node in ast.walk(embedded))
        sources = [item for item in unique if classify("source", item, python_source)] if source_values else []
        if sources:
            source_ids = {id(item) for item in sources}
            return (*sources, *choices((item for item in unique if id(item) not in source_ids), row_cache=row_cache))
        # Keep canonical helper/action witnesses concrete. Widening known
        # read-only actions would invent a dynamic action and a false refusal.
        witnesses, remaining = {}, []
        for item in unique:
            witness = classify("witness", item, helper_witness)
            if witness is None:
                remaining.append(item)
            else:
                witnesses.setdefault(witness, item)
        result = list(unique[:7])
        for item in witnesses.values():
            if not any(same(item, prior) for prior in result):
                result.append(item)
        if remaining:
            result.append(widen(remaining))
        return tuple(result)

    def parsed_source(source, mode):
        key = source, mode
        if key not in parsed_sources:
            try:
                parsed_sources[key] = ast.parse(source, mode=mode)
            except (SyntaxError, ValueError):
                parsed_sources[key] = None
        return parsed_sources[key]

    def python_source_prefix(row):
        if not isinstance(row, (list, tuple)) or not all(isinstance(word, str) for word in row):
            return None
        child = unwrapped_command(row, unwrap_code=False)
        if not child or not re.fullmatch(r"python[0-9.]*", Path(child[0]).name) or "-c" not in child:
            return None
        position = child.index("-c") + 1
        if position >= len(child) or parsed_source(child[position], "exec") is None:
            return None
        positions = [index + 1 for index, word in enumerate(row[:-1])
                     if word == "-c" and row[index + 1] == child[position]]
        return tuple(row[:positions[0] + 1]) if len(positions) == 1 else None

    def source_alternative(sources):
        """Keep complete source choices as branches instead of a string product."""
        if len(sources) == 1:
            return sources[0]
        def bodies(modules):
            if len(modules) == 1:
                return modules[0].body or [ast.Pass()]
            middle = len(modules) // 2
            return [ast.If(test=ast.Name(id="_source_alternative", ctx=ast.Load()),
                           body=bodies(modules[:middle]), orelse=bodies(modules[middle:]))]
        module = ast.Module(body=bodies([parsed_source(source, "exec") for source in sources]), type_ignores=[])
        rendered = ast.unparse(ast.fix_missing_locations(module)) + "\n"
        factored_sources.add(rendered)
        return rendered

    def combinations(parts, initial=None, row_cache=None):
        rows = [()] if initial is None else list(initial)
        for part in parts:
            compact = None
            combined = []
            for row in rows:
                child = unwrapped_command(row) if all(isinstance(word, str) for word in row) else []
                if child and re.fullmatch(r"python[0-9.]*", Path(child[0]).name) and child[-1] == "-c":
                    if compact is None:
                        sources = [item for item in part if isinstance(item, str) and parsed_source(item, "exec") is not None]
                        strings = set(sources)
                        compact = ([source_alternative(sources), *(item for item in part if not isinstance(item, str) or item not in strings)]
                                   if len(sources) > 8 else part)
                    operands = compact
                elif child and re.fullmatch(r"python[0-9.]*", Path(child[0]).name) and "-c" in child:
                    # Later argv words are data to the selected Python source.
                    # Bound them before multiplying rows, even if their text
                    # happens to resemble another Python program.
                    operands = choices(part, source_values=False, row_cache=row_cache)
                else:
                    operands = part
                combined.extend([*row, item] for item in operands)
            rows = list(choices(combined, row_cache=row_cache))
        return rows

    def function_names(node, scope):
        if isinstance(node, ast.Name):
            bound = scope.get(node.id)
            if bound is None:
                bound = patched_attributes.get("builtins." + node.id)
            return ({item.name for item in bound if isinstance(item, ast.alias)}
                    | {item.kind.name for item in bound if isinstance(item, Instance) and isinstance(item.kind, ast.alias)}) if bound else {node.id}
        if isinstance(node, ast.Attribute):
            names = {parent + "." + node.attr for parent in function_names(node.value, scope)}
            return {alias.name if isinstance(alias, ast.alias) else name
                    for name in names for alias in patched_attributes.get(name, (ast.alias(name=name),))
                    if isinstance(alias, ast.alias) or alias is unknown}
        if isinstance(node, ast.Call):
            return function_names(node.func, scope)
        if isinstance(node, ast.Constant):
            return {type(node.value).__name__}
        return set()

    def class_member(kind, name, seen=()):
        if id(kind) in seen:
            return ()
        assigned = class_bindings.get(id(kind), {}).get(name, ())
        if assigned and not any(item is unknown for item in assigned):
            return assigned
        for base in class_bases.get(id(kind), ()):
            inherited = class_member(base, name, (*seen, id(kind)))
            if inherited:
                return choices((*assigned, *inherited))
        return assigned

    def class_names(kind, seen=()):
        if id(kind) in seen:
            return set()
        return set(class_bindings.get(id(kind), {})).union(*(class_names(base, (*seen, id(kind)))
                                                          for base in class_bases.get(id(kind), ())))

    def json_native_kind(kind, seen=()):
        if id(kind) in seen:
            return None
        if isinstance(kind, ast.alias) and kind.name.removeprefix("builtins.") in {"str", "int", "float", "list", "tuple", "dict"}:
            return kind.name.removeprefix("builtins.")
        return next((name for base in class_bases.get(id(kind), ())
                     if (name := json_native_kind(base, (*seen, id(kind))))), None)

    def json_native_protocols(item, scope, *, skip_empty=False):
        native = json_native_kind(item.kind)
        if native not in {"list", "tuple", "dict"}:
            return ()
        if skip_empty and (member(item, "__bool__", scope, special=True) or member(item, "__len__", scope, special=True)):
            return (Bound(ast.alias(name="json.container"), item),)
        if (native == "dict" or skip_empty) and item.json_payload and all(
                isinstance(child, Keywords) and not child.items or isinstance(child, Sequence) and all(not row for row in sequence_rows(child))
                for child in item.json_payload):
            return ()
        return member(item, "items" if native == "dict" else "__iter__", scope, special=native != "dict")

    def wrapped_functions(callback):
        if isinstance(callback, Function):
            return (callback,)
        if isinstance(callback, Descriptor):
            return wrapped_functions(callback.function)
        if isinstance(callback, Bound):
            return tuple(function for item in callback.receiver for function in wrapped_functions(item)) if isinstance(callback.function, PatchContext) else wrapped_functions(callback.function)
        return ()

    def member(owner, name, scope=None, *, special=False):
        nonlocal attribute_mutations
        if isinstance(owner, Descriptor) and owner.kind == "property" and name in {"getter", "setter", "deleter"}:
            return (Descriptor(owner, name),)
        if isinstance(owner, Descriptor) and owner.kind == "property" and name in {"fget", "fset", "fdel"}:
            return (owner.function,) if name == "fget" else owner.setter if name == "fset" else owner.deleter
        receiver = owner.receiver if isinstance(owner, Super) else owner
        kind = receiver.kind if isinstance(receiver, Instance) else receiver
        special = special and not (isinstance(owner, Instance) and isinstance(kind, ast.alias)
                                   and kind.name in mock_types and name in owner.attributes)
        if (not special and isinstance(owner, Instance) and isinstance(kind, ast.alias) and kind.name in mock_types
                and name not in owner.attributes and unknown in owner.attributes):
            return choices((unknown, *owner.attributes[unknown]))
        assigned = class_member(kind, name)
        if isinstance(owner, Super):
            assigned = next((matched for base in class_bases.get(id(owner.kind), ()) if (matched := class_member(base, name))), ())
        properties = tuple(item for item in assigned if isinstance(item, Descriptor) and item.kind == "property")
        shadowed = ()
        if not special and isinstance(owner, Instance) and name in owner.attributes and not properties:
            if not any(item is unknown for item in owner.attributes[name]):
                return owner.attributes[name]
            shadowed = owner.attributes[name]
        if not special and properties and isinstance(owner, Instance) and name in owner.attributes:
            if len(properties) != len(assigned):
                shadowed = owner.attributes[name]
            assigned = properties
        result = list(shadowed)
        for item in assigned:
            if isinstance(item, Descriptor):
                if item.kind == "classmethod":
                    item = Bound(item.function, kind)
                elif item.kind == "staticmethod":
                    item = item.function
                elif isinstance(receiver, Instance) and scope is not None:
                    returned = invoke(Bound(item.function, receiver), scope, evaluated=True)
                    result.extend(returned)
                    if item.kind == "cached_property":
                        attribute_mutations += 1
                        observe(receiver.attributes)
                        receiver.attributes[name] = returned
                    continue
            elif (isinstance(item, Function) or isinstance(item, Bound) and isinstance(item.function, PatchContext)) and isinstance(receiver, Instance):
                item = Bound(item, receiver)
            result.append(item)
        return choices(result)

    def comprehension(node, scope, outer=None):
        local = Scope(scope)
        for index, generator in enumerate(node.generators):
            if index == 0 and outer is not None:
                items = iterate_values(outer, scope)
            else:
                scan(generator.iter, local)
                items = iterated(generator.iter, local)
            if not items:
                return None
            bind(generator.target, items, local)
            for condition in generator.ifs:
                scan(condition, local)
                if isinstance(condition, ast.Constant) and not condition.value:
                    return None
        return local

    def generator_values(sequence, scope):
        if sequence.state["exhausted"] == (True,):
            return ()
        if sequence.operands is not None:
            items = choices(item for operand in sequence.operands for item in iterate_values((operand,), sequence.scope))
            return choices((unknown, item) for item in items) if sequence.arguments == ("enumerate",) else items
        if isinstance(sequence.node, ast.GeneratorExp):
            local = comprehension(sequence.node, sequence.scope, sequence.arguments[0])
            if local is None:
                return ()
            scan(sequence.node.elt, local)
            return value(sequence.node.elt, local)
        if isinstance(sequence.node, ast.Call):
            functions, names, inputs = sequence.arguments
            arguments = [iterate_values(assigned, sequence.scope) for assigned in inputs]
            if any(not assigned for assigned in arguments):
                return ()
            results = [item for function in functions for item in invoke(function, sequence.scope, arguments, evaluated=True)]
            if not results and len(arguments) == 1 and names & {"str", "repr", "ascii", "shlex.quote"}:
                operation = shlex.quote if "shlex.quote" in names else repr if "repr" in names else ascii if "ascii" in names else str
                results = [operation(item if isinstance(item, (str, int, float, bool)) else computed) for item in arguments[0]]
            return choices(results) or (unknown,)
        yielded = {}
        result = invoke(sequence.node, sequence.scope, sequence.arguments, sequence.keywords, evaluated=True, consumed=True, yielded=yielded, created_active=sequence.created_active)
        wrapper = sequence.node.function if isinstance(sequence.node, Bound) else sequence.node
        if isinstance(wrapper, Function) and isinstance(wrapper.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            paths = yield_paths(wrapper.node.body)
            if paths is not None:
                sequence.positions = [choices(item for path in paths.values() if index < len(path)
                                              for identity in path[index] for item in yielded.get(identity, (unknown,)))
                                      for index in range(max(paths, default=0))]
        return result

    def yield_paths(statements):
        # Merge equal-length alternatives by position. Independent branches
        # must not expand into every possible complete execution path.
        def retain(rows, path):
            length = len(path)
            rows[length] = tuple(before | after for before, after in zip(rows[length], path)) if length in rows else path
        paths = {0: ()}
        for statement in statements:
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Yield):
                alternatives = {1: (frozenset({id(statement)}),)}
            elif isinstance(statement, ast.If):
                bodies = [statement.body, statement.orelse]
                if isinstance(statement.test, ast.Constant):
                    bodies = [bodies[0] if statement.test.value else bodies[1]]
                branches = [yield_paths(body) for body in bodies]
                if any(branch is None for branch in branches):
                    return None
                alternatives = {}
                for branch in branches:
                    for path in branch.values():
                        retain(alternatives, path)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            elif any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(statement)):
                return None
            else:
                continue
            combined = {}
            for before in paths.values():
                for after in alternatives.values():
                    retain(combined, (*before, *after))
            paths = combined
        return paths

    def is_value_iterator(item):
        return isinstance(item, Instance) and isinstance(item.kind, ast.alias) and item.kind.name in {"ownership.iter", "ownership.cycle"}

    def make_iterator(source, cycle=False):
        iterator = Instance(ast.alias(name="ownership.cycle" if cycle else "ownership.iter"))
        iterator.attributes.update(source=source, states=(Sequence(((0, (), False),)),))
        return iterator

    def iterator_rows(iterator, scope):
        source = iterator.attributes["source"]
        if all(isinstance(item, (Sequence, list, tuple, str, Keywords)) for item in source):
            return [list(item) if isinstance(item, str) else list(item.items) if isinstance(item, Keywords) else list(row)
                    for item in source for row in (sequence_rows(item) if isinstance(item, (Sequence, list, tuple)) else (item,))]
        if "materialized" not in iterator.attributes:
            rows = iterable_rows(source, scope)
            for item in source:
                if isinstance(item, Generator):
                    observe(item.state)
                    item.state["partial"] = (True,)
            observe(iterator.attributes)
            iterator.attributes["materialized"] = (Sequence(rows),)
        return [list(row) for sequence in iterator.attributes["materialized"] for row in sequence_rows(sequence)]

    def consume_iterator(iterator, scope):
        nonlocal attribute_mutations
        states = [row for assigned in iterator.attributes["states"] for row in sequence_rows(assigned)]
        results, updated = [], []
        cycle = iterator.kind.name == "ownership.cycle"
        source = iterator.attributes["source"]
        if len(source) == 1 and is_value_iterator(source[0]):
            operands = consume_iterator(source[0], scope) if any(len(state) == 3 and not state[2] for state in states) else ()
            for state in states:
                if len(state) != 3 or not isinstance(state[0], int) or not isinstance(state[1], tuple):
                    results.append(unknown)
                    continue
                index, saved, cycling = state
                if not cycling and operands:
                    results.extend(operands)
                    updated.extend((index + 1, (*saved, item) if cycle else (), False) for item in operands)
                elif cycle and saved:
                    position = index if cycling else 0
                    results.append(saved[position % len(saved)])
                    updated.append((position + 1, saved, True))
                else:
                    updated.append(state)
            attribute_mutations += 1
            observe(iterator.attributes)
            iterator.attributes["states"] = (Sequence(updated),)
            return choices(results)
        rows = iterator_rows(iterator, scope)
        for row in rows:
            for state in states:
                if len(state) != 3 or not isinstance(state[0], int) or not isinstance(state[1], tuple):
                    results.append(unknown)
                    continue
                index, saved, cycling = state
                if not cycling and index < len(row):
                    results.append(row[index])
                    updated.append((index + 1, (*saved, row[index]) if cycle else (), False))
                elif cycle and saved:
                    position = index if cycling else 0
                    results.append(saved[position % len(saved)])
                    updated.append((position + 1, saved, True))
                else:
                    updated.append((index, saved, cycling))
        attribute_mutations += 1
        observe(iterator.attributes)
        iterator.attributes["states"] = (Sequence(updated),)
        return choices(results)

    def remaining_iterator_rows(iterator, scope):
        nonlocal attribute_mutations
        rows = iterator_rows(iterator, scope)
        states = [row for assigned in iterator.attributes["states"] for row in sequence_rows(assigned)]
        remaining = [list(state[1]) if state[2] else row[state[0]:]
                     for row in rows for state in states if len(state) == 3 and isinstance(state[0], int)]
        observe(iterator.attributes)
        attribute_mutations += 1
        iterator.attributes["states"] = (Sequence(((max(map(len, rows), default=0), (), False),)),)
        return remaining

    def iterate_values(sequences, scope):
        nonlocal recursion_skips
        result = []
        for sequence in sequences:
            if is_value_iterator(sequence):
                # Whole-iterable consumers inspect one finite pass even for a
                # cycle. Mock/next consumers advance individual positions.
                result.extend(item for row in remaining_iterator_rows(sequence, scope) for item in row)
            elif isinstance(sequence, Generator):
                identity = id(sequence)
                if identity in active_iterators:
                    recursion_skips += 1
                    result.append(unknown)
                    continue
                active_iterators.add(identity)
                try:
                    result.extend(generator_values(sequence, scope))
                finally:
                    active_iterators.remove(identity)
            elif isinstance(sequence, Sequence):
                result.extend(sequence_members(sequence))
            elif isinstance(sequence, (list, tuple)):
                result.extend(sequence)
            elif isinstance(sequence, Keywords):
                result.extend(sequence.items)
            elif isinstance(sequence, str) and overflow in sequence:
                result.append(overflow)
            else:
                result.append(unknown)
        return choices(result)

    def iterated(node, scope):
        if isinstance(node, ast.Call) and "map" in function_names(node.func, scope) and len(node.args) > 1:
            return iterate_values(value(node, scope), scope)
        if isinstance(node, ast.GeneratorExp):
            local = comprehension(node, scope)
            if local is None:
                return ()
            scan(node.elt, local)
            return value(node.elt, local)
        assigned = value(node, scope)
        for item in assigned:
            if isinstance(item, Generator):
                observe(item.state)
                item.state["partial"] = (True,)
        return iterate_values(assigned, scope)

    def generator_suffixes(sequence, rows):
        if True in sequence.state["partial"]:
            return [row[index:] for row in rows for index in range(len(row) + 1)]
        return rows

    def iterable_rows(assigned, scope):
        """Retain ordered argv rows when a consumer copies or renders them."""
        rows = []
        for sequence in assigned:
            if isinstance(sequence, Generator) and sequence.state["exhausted"] == (True,):
                rows.append([])
                continue
            if is_value_iterator(sequence):
                rows.extend(remaining_iterator_rows(sequence, scope))
                continue
            if isinstance(sequence, (Sequence, list, tuple)):
                rows.extend(list(row) for row in sequence_rows(sequence))
                continue
            if isinstance(sequence, Generator) and isinstance(sequence.node, ast.GeneratorExp) and len(sequence.node.generators) == 1:
                captures = sequence.arguments[0]
                if all(isinstance(source, (Sequence, list, tuple)) for source in captures):
                    generated = []
                    generator = sequence.node.generators[0]
                    for source in captures:
                        for source_row in sequence_rows(source):
                            words = []
                            for item in source_row:
                                local = Scope(sequence.scope)
                                bind(generator.target, (item,), local)
                                if any(isinstance(condition, ast.Constant) and not condition.value for condition in generator.ifs):
                                    continue
                                for condition in generator.ifs:
                                    scan(condition, local)
                                scan(sequence.node.elt, local)
                                words.append(value(sequence.node.elt, local))
                            generated.extend(combinations(words))
                    rows.extend(generator_suffixes(sequence, generated))
                    continue
            if isinstance(sequence, Generator) and isinstance(sequence.node, ast.Call):
                functions, names, inputs = sequence.arguments
                if all(isinstance(source, (Sequence, list, tuple)) for assigned in inputs for source in assigned):
                    generated = []
                    alternatives = [tuple(row for source in assigned for row in sequence_rows(source)) for assigned in inputs]
                    for source_rows in combinations(alternatives):
                        words = []
                        for items in zip(*source_rows):
                            arguments = [(item,) for item in items]
                            mapped = choices(result for function in functions for result in invoke(function, sequence.scope, arguments, evaluated=True))
                            if not mapped and len(items) == 1 and names & {"str", "repr", "ascii", "shlex.quote"}:
                                operation = shlex.quote if "shlex.quote" in names else repr if "repr" in names else ascii if "ascii" in names else str
                                item = items[0]
                                mapped = (operation(item if isinstance(item, (str, int, float, bool)) else computed),)
                            words.append(mapped or (unknown,))
                        generated.extend(combinations(words))
                    rows.extend(generator_suffixes(sequence, generated))
                    continue
            rows.append(list(iterate_values((sequence,), scope)))
        return rows

    def unresolved_text(item):
        return isinstance(item, str) and (computed in item or overflow in item)

    @lru_cache(maxsize=512)
    def executable_operand(text):
        return overflow in text or executable_content(text) or opaque_ownership("exec_command", {"cmd": text})

    def put_mapping(items, key, assigned):
        if key is unknown:
            items[key] = choices((*items.get(key, ()), *assigned))
        else:
            items[key] = assigned

    def update_mapping(items, mapping):
        for key, assigned in mapping.items():
            put_mapping(items, key, assigned)

    def make_mock(kind, keywords):
        mock = Instance(ast.alias(name=kind))
        mock.attributes.update({"_mock_wraps" if name == "wraps" else name: assigned
                                for name, assigned in keywords.items()})
        wildcard = mock.attributes.get(unknown, ())
        if wildcard:
            # Construction takes a snapshot. Later explicit assignments replace
            # these possibilities instead of resurrecting constructor values.
            for name in tuple(mock.attributes):
                if name is not unknown:
                    mock.attributes[name] = choices((*mock.attributes[name], *wildcard))
            for name, default in (("side_effect", (None,)),
                                  ("return_value", (ast.alias(name="unittest.mock.DEFAULT"),)),
                                  ("_mock_wraps", (None,))):
                if name not in mock.attributes:
                    mock.attributes[name] = choices((*default, *wildcard))
        return mock

    def patch_replacement(replacement, keywords, scope):
        inject = not replacement or any(isinstance(item, ast.alias) and item.name == "unittest.mock.DEFAULT" for item in replacement)
        if inject:
            if "new_callable" in keywords:
                options = {name: assigned for name, assigned in keywords.items()
                           if name not in {"target", "attribute", "new", "new_callable", "spec", "spec_set", "create", "autospec"}}
                replacement = choices(item for factory in keywords["new_callable"]
                                      for item in invoke(factory, scope, keywords=options, evaluated=True)) or (unknown,)
            else:
                replacement = (make_mock("unittest.mock.MagicMock", {name: assigned for name, assigned in keywords.items()
                                                                 if name in {"side_effect", "return_value", "wraps"}}),)
        return replacement, inject

    def consume_deferred(assigned, scope, memo=None):
        # Async functions already expose completion values in this model.
        # Preserve those values alongside callbacks that have not run yet.
        memo = {} if memo is None else memo
        results = []
        for operand in assigned:
            if isinstance(operand, Deferred):
                if id(operand) not in memo:
                    memo[id(operand)] = (operand, invoke(operand.callback, scope, evaluated=True, consumed=True))
                results.extend(memo[id(operand)][1])
            else:
                results.append(operand)
        return choices(results) or (None,)

    def deferred_operands(assigned, seen=None):
        seen = set() if seen is None else seen
        for operand in assigned:
            if id(operand) in seen:
                continue
            seen.add(id(operand))
            if isinstance(operand, Deferred):
                yield operand
            elif isinstance(operand, Keywords):
                for child in operand.items.values():
                    yield from deferred_operands(child, seen)
            elif isinstance(operand, Sequence):
                yield from deferred_operands(sequence_members(operand), seen)
            elif isinstance(operand, (list, tuple)):
                yield from deferred_operands(operand, seen)

    def async_result(name, arguments, keywords, scope):
        if name == "asyncio.gather":
            memo = {}
            return (Sequence(combinations([consume_deferred(assigned, scope, memo) for assigned in arguments])),)
        parameter = async_calls[name]
        return consume_deferred(arguments[0] if arguments else keywords.get(parameter, ()), scope)

    def object_patch_context(parent, attribute, replacement, inject, create=False):
        if not isinstance(attribute, str):
            return PatchContext(computed, replacement, inject, attribute_owner=unknown, create=create)
        if isinstance(parent, (ast.alias, str)):
            return PatchContext((parent.name if isinstance(parent, ast.alias) else parent) + "." + attribute, replacement, inject, create=create)
        return PatchContext(attribute, replacement, inject, attribute_owner=parent, create=create)

    def patch_binding(context):
        if isinstance(context.attribute_owner, ast.ClassDef):
            return class_bindings.setdefault(id(context.attribute_owner), {}), context.target
        if isinstance(context.attribute_owner, Instance):
            return context.attribute_owner.attributes, context.target
        if context.attribute_owner is not None:
            return None, context.target
        return patched_attributes, context.target

    def string_transform(node, scope, names):
        methods = {"strip", "lstrip", "rstrip", "replace", "format"}
        dedent = "textwrap.dedent" in names
        method = node.func.attr if isinstance(node.func, ast.Attribute) and node.func.attr in methods else None
        if not dedent and method is None:
            return None
        for argument in node.args:
            if isinstance(argument, ast.Starred):
                sequences = value(argument.value, scope)
                if not sequences or any(not isinstance(sequence, (Sequence, list, tuple)) for sequence in sequences):
                    return None
                rows = [row for sequence in sequences for row in sequence_rows(sequence)]
                if any(not isinstance(row, (list, tuple)) for row in rows) or len({len(row) for row in rows}) != 1:
                    return None
        for keyword in node.keywords:
            if keyword.arg is None:
                mappings = value(keyword.value, scope)
                if not mappings or any(not isinstance(mapping, Keywords)
                        or any(not isinstance(key, str) for key in mapping.items) for mapping in mappings):
                    return None
        arguments = expanded_arguments(node.args, scope)
        keywords = call_keywords(node.keywords, scope)
        if any(not isinstance(name, str) for name in keywords):
            return None
        if dedent:
            receivers = (None,)
        elif names & {"str." + method, "builtins.str." + method} and all(isinstance(owner, ast.alias) and owner.name in {"str", "builtins.str"} for owner in value(node.func.value, scope)):
            if not arguments:
                return None
            receivers, arguments = arguments[0], arguments[1:]
        else:
            receivers = value(node.func.value, scope)
        if not dedent and any(not isinstance(item, str) and item is not unknown for item in receivers):
            return None
        if method == "format" and (any(type(item) not in (str, int, float, bool, type(None)) or unresolved_text(item)
                for assigned in [*arguments, *keywords.values()] for item in assigned)
                or any(item is unknown or unresolved_text(item) for item in receivers)):
            alternatives = [(receiver, row) for receiver in receivers
                            for row in combinations([*arguments, *keywords.values()])]
            def render(alternative):
                receiver, row = alternative
                if receiver is unknown or unresolved_text(receiver):
                    return unknown_template_effects(row, scope, template=receiver)
                if not isinstance(receiver, str):
                    return (unknown,)
                positional = [conversion_operand(item, scope) for item in row[:len(arguments)]]
                named = {key: conversion_operand(item, scope) for key, item in zip(keywords, row[len(arguments):])}
                try:
                    return (receiver.format(*positional, **named),)
                except InvalidTextConversion:
                    return ()
                except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
                    return format_failure(receiver, row)
            return evaluation_alternatives(alternatives, scope, render)
        operands = [receivers, *arguments, *keywords.values()]
        known = [tuple(item for item in assigned if type(item) in (str, int, float, bool, type(None))
                       and not unresolved_text(item))
                 for assigned in operands]
        uncertain = [index for index, (literals, assigned) in enumerate(zip(known, operands))
                     if len(literals) != len(assigned)]
        results = []
        if uncertain:
            results.extend(item for assigned in operands for item in assigned if unresolved_text(item))
            # Keep code from another operand when the unresolved operation
            # could return it; finite unary source tables remain concrete.
            results.append(overflow if any(isinstance(item, str) and executable_operand(item)
                for index in uncertain for other, assigned in enumerate(known) if other != index
                for item in assigned) else unknown)
        combinations_count = 1
        for assigned in known:
            combinations_count *= len(assigned)
        if combinations_count > 8 and sum(len(assigned) > 1 for assigned in known) > 1:
            return choices((*results, overflow))
        for row in product(*known):
            receiver, positional = row[0], row[1:1 + len(arguments)]
            named = dict(zip(keywords, row[1 + len(arguments):]))
            if not dedent and not isinstance(receiver, str):
                results.append(unknown)
                continue
            try:
                rendered = textwrap.dedent(*positional, **named) if dedent else getattr(receiver, method)(*positional, **named)
            except (TypeError, ValueError, KeyError, IndexError, AttributeError):
                results.append(overflow if any(isinstance(item, str) and item in factored_sources for item in row) else unknown)
                continue
            sources = [item for item in row if isinstance(item, str) and item in factored_sources]
            if sources:
                parsed = parsed_source(rendered, "exec")
                if parsed is None or any(ast.dump(parsed) != ast.dump(parsed_source(source, "exec")) for source in sources):
                    results.append(overflow)
                    continue
                factored_sources.add(rendered)
            results.append(rendered)
        return choices(results) or (unknown,)

    def evaluation_alternatives(alternatives, scope, render):
        """Alternative evaluations execute from the same state, not in sequence."""
        if len(alternatives) <= 1:
            return choices(text for alternative in alternatives for text in render(alternative))
        rendered, states = [], []
        for alternative in alternatives:
            states.append(branch((), scope, executor=lambda local: rendered.extend(render(alternative))))
        scope.state = merge(*states)
        return choices(rendered)

    def uncertain_protocol_effects(operands, scope, callbacks=(), *, zero_result=None):
        """Unknown protocol order/count keeps zero calls and reachable states."""
        def havoc():
            nonlocal attribute_mutations
            pending = [*operands, *(callback for callback, _ in callbacks)]
            seen = set()
            while pending:
                item = pending.pop()
                if id(item) in seen:
                    continue
                seen.add(id(item))
                if isinstance(item, Bound):
                    pending.extend((item.function, item.receiver))
                elif isinstance(item, Partial):
                    pending.extend(item.functions)
                    pending.extend(child for assigned in (*item.arguments, *item.keywords.values()) for child in assigned)
                elif isinstance(item, Function):
                    for name in item.free | item.globals | item.nonlocals:
                        owner = item.scope.owner(name)
                        if owner is not None:
                            assigned = owner.get(name, ())
                            pending.extend(assigned)
                            owner[name] = choices((*assigned, unknown))
                elif isinstance(item, Instance):
                    observe(item.attributes)
                    for name, assigned in list(item.attributes.items()):
                        pending.extend(assigned)
                        item.attributes[name] = choices((*assigned, unknown))
                elif isinstance(item, Keywords):
                    observe(item.state)
                    for name, assigned in list(item.items.items()):
                        pending.extend(assigned)
                        item.items[name] = choices((*assigned, unknown))
                elif isinstance(item, Sequence):
                    members = tuple(sequence_members(item))
                    pending.extend(members)
                    observe(item.state)
                    item.state['rows'] = choices((*item.state['rows'], *([child] for child in members), [unknown]))
                elif isinstance(item, (list, tuple)):
                    pending.extend(item)
            attribute_mutations += 1
            expression_values.clear()
        havoc()
        actions = (lambda local: (unknown,) if zero_result is None else zero_result, *(lambda local, callback=callback, args=args:
                   invoke(callback, local, args, evaluated=True) for callback, args in callbacks))
        result = evaluation_alternatives(actions, scope, lambda action: action(scope))
        havoc()
        return result


    def unknown_template_effects(operands, scope, *, percent=False, template=None):
        """An unknown field list cannot prove a callback count or conversion."""
        def callbacks_for(values):
            pending, seen, callbacks = list(values), set(), []
            while pending:
                item = pending.pop()
                if id(item) in seen:
                    continue
                seen.add(id(item))
                if isinstance(item, Instance):
                    protocols = (("__str__", ()), ("__repr__", ()),
                                 ("__int__", ()), ("__index__", ()), ("__float__", ()),
                                 ("__getitem__", ((unknown,),))) if percent else (
                                     ("__format__", ((computed,),)), ("__str__", ()), ("__repr__", ()),
                                     ("__getitem__", ((unknown,),)), ("__getattribute__", ((unknown,),)),
                                     ("__getattr__", ((unknown,),)))
                    for name, arguments in protocols:
                        callbacks.extend((callback, arguments) for callback in member(item, name, special=True))
                    pending.extend(child for assigned in item.attributes.values() for child in assigned)
                    if not percent:
                        for name in class_names(item.kind):
                            for child in class_member(item.kind, name):
                                if isinstance(child, Descriptor) and child.kind in {"property", "cached_property"}:
                                    callbacks.append((Bound(child.function, item), ()))
                                elif isinstance(child, (Instance, Sequence, Keywords, list, tuple)):
                                    pending.append(child)
                elif isinstance(item, Keywords):
                    pending.extend(child for assigned in item.items.values() for child in assigned)
                elif isinstance(item, Sequence):
                    pending.extend(sequence_members(item))
                elif isinstance(item, (list, tuple)):
                    pending.extend(item)
            return callbacks

        returned = protocol_return_effects(operands, scope, callbacks_for)
        return (overflow if any(conversion_witness(item) for item in (template, *operands, *returned)) else unknown,)

    def protocol_return_effects(operands, scope, callbacks_for, outputs=None, receivers_for=None):
        """Follow returned protocol receivers using the existing source-kind join."""
        if outputs is None:
            outputs = uncertain_protocol_effects(operands, scope, callbacks_for(operands), zero_result=())
        returned, representatives, analyzed, captured, kind_keys, opaque_values = list(outputs), {}, {}, {}, {}, {}
        def kind_key(kind):
            if isinstance(kind, (Function, Generator)):
                return kind_key(kind.node)
            if isinstance(kind, Bound):
                return kind_key(kind.function)
            key = id(kind)
            if key not in kind_keys:
                kind_keys[key] = ast.dump(kind) if isinstance(kind, ast.AST) else repr(kind)
            return kind_keys[key]

        def receiver_key(item):
            if isinstance(item, Instance):
                return "instance", kind_key(item.kind)
            if isinstance(item, (Function, Bound, Generator, ast.AST)):
                return type(item).__name__, kind_key(item)
            if isinstance(item, Partial):
                return "partial", tuple(receiver_key(callback) for callback in item.functions)
            return "opaque", type(item).__name__

        def capture_fields(receivers):
            pending, seen = [callback for callback, _ in callbacks_for(receivers)], set()
            pending.extend(child for item in receivers if isinstance(item, (Instance, ast.ClassDef))
                           for kind in (item.kind if isinstance(item, Instance) else item,)
                           for name in class_names(kind) for child in class_member(kind, name))
            pending.extend(item for item in receivers if isinstance(item, (Function, Bound, Partial, Generator)))
            def capture(source, bindings):
                for name, assigned in bindings.items():
                    pending.extend(assigned)
                    values = tuple(kind_key(item) if isinstance(item, ast.ClassDef)
                                   else ("instance", kind_key(item.kind)) if isinstance(item, Instance)
                                   else ast.dump(item.node) if isinstance(item, Function)
                                   else receiver_key(item) if isinstance(item, (Generator, Bound, Partial)) else item for item in assigned)
                    key = source, name
                    captured[key] = choices((*captured.get(key, ()), *values), source_values=False)
            while pending:
                callback = pending.pop()
                if id(callback) in seen:
                    continue
                seen.add(id(callback))
                if isinstance(callback, Bound):
                    pending.extend((callback.function, callback.receiver))
                elif isinstance(callback, Partial):
                    pending.extend(callback.functions)
                    capture(str(receiver_key(callback)), {**{str(index):assigned for index,assigned in enumerate(callback.arguments)}, **callback.keywords})
                elif isinstance(callback, Generator):
                    if isinstance(callback.node, Function):
                        pending.append(callback.node)
                    elif isinstance(callback.node, ast.AST):
                        def loaded(node):
                            return {child.id for child in ast.walk(node) if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)}
                        names = loaded(callback.node)
                        if isinstance(callback.node, ast.GeneratorExp):
                            names, bound = set(), set()
                            for generator in callback.node.generators:
                                names.update(loaded(generator.iter) - bound)
                                bound.update(child.id for child in ast.walk(generator.target) if isinstance(child, ast.Name))
                                for condition in generator.ifs:
                                    names.update(loaded(condition) - bound)
                            names.update(loaded(callback.node.elt) - bound)
                        capture(kind_key(callback.node), {name:callback.scope.get(name, ()) for name in names})
                elif isinstance(callback, Instance):
                    capture("instance:" + str(kind_key(callback.kind)), callback.attributes)
                elif isinstance(callback, Function):
                    source = ast.dump(callback.node)
                    captured.setdefault((source, ""), (None,))
                    capture(source, {**{"=" + name: assigned for name, assigned in callback.defaults.items()},
                                     **{name: callback.scope.get(name, ()) for name in callback.free}})
            return tuple(sorted((key, binding_key(assigned)) for key, assigned in captured.items()))
        # Attribute paths can continue through objects returned by getters.
        # Join their receiver fields by kind before another callback pass;
        # fresh allocations of the same shape do not create an endless walk.
        def join_instance(item):
            if not isinstance(item, Instance):
                return item
            key = receiver_key(item)
            representative = representatives.setdefault(key, Instance(item.kind))
            if representative is item or id(item) in joining:
                return representative
            joining.add(id(item))
            for name, assigned in item.attributes.items():
                representative.attributes[name] = choices((*representative.attributes.get(name, ()),
                                                            *(join_instance(child) for child in assigned)))
            joining.remove(id(item))
            return representative

        def fields(item):
            if isinstance(item, Instance):
                return tuple(sorted((binding_key(name), binding_key(assigned)) for name, assigned in item.attributes.items()))
            if isinstance(item, Sequence):
                return binding_key(item.state["rows"])
            if isinstance(item, Generator):
                return binding_key(tuple(sorted(item.state.items())))
            return binding_key(opaque_values.get(receiver_key(item), ()))

        while outputs:
            joining = set()
            pending, new_receivers, seen = list(receivers_for(outputs) if receivers_for else outputs), [], set()
            while pending:
                item = pending.pop()
                if id(item) in seen:
                    continue
                seen.add(id(item))
                if isinstance(item, Instance):
                    new_receivers.append(item)
                    join_instance(item)
                elif receivers_for:
                    new_receivers.append(item)
                    key = receiver_key(item)
                    representative = representatives.setdefault(key, Sequence((), unique=item.unique) if isinstance(item, Sequence) else item)
                    if isinstance(item, Sequence):
                        representative.state["rows"] = choices((*representative.state["rows"], *item.state["rows"]))
                    elif type(item) in (bytes, complex):
                        opaque_values[key] = choices((*opaque_values.get(key, ()), item), source_values=False)
                elif isinstance(item, Sequence):
                    pending.extend(sequence_members(item))
                elif isinstance(item, Keywords):
                    pending.extend(child for assigned in item.items.values() for child in assigned)
                elif isinstance(item, (list, tuple)):
                    pending.extend(item)
            captures = capture_fields(new_receivers)
            dirty = [(key, item) for key, item in representatives.items()
                     if analyzed.get(key) != (fields(item), captures)]
            if not dirty:
                break
            for key, item in dirty:
                analyzed[key] = fields(item), captures
            kinds = {key for key, _ in dirty}
            receivers = (*tuple(item for _, item in dirty),
                         *(item for item in new_receivers if receiver_key(item) in kinds))
            outputs = uncertain_protocol_effects((*operands, *receivers), scope, callbacks_for(receivers), zero_result=())
            returned.extend(outputs)
        return choices(returned)

    def format_failure(template, operands, percent=False):
        """Retain known command scaffolding when an operand is unresolved."""
        try:
            for item in operands:
                data_literal(item)
        except InexactDataLoop:
            pass
        else:
            return ()
        marker = overflow if any(conversion_witness(item) for item in operands) else computed
        if percent:
            rendered = re.sub(r"%(?:\([^)]+\))?[#0 +\-]*(?:\*|[0-9]+)?(?:\.(?:\*|[0-9]+))?[hlL]?[diouxXeEfFgGcrsa%]",
                              lambda match: "%" if match[0] == "%%" else marker, template)
        else:
            try:
                rendered = "".join(literal + (marker if field is not None else "")
                                   for literal, field, spec, conversion in string.Formatter().parse(template))
            except ValueError:
                rendered = template + marker
        return (rendered if not conversion_witness(template) or executable_operand(rendered) else overflow,)

    def text_conversion(item, operation, scope, spec="", seen=()):
        """Render generic values without treating uncertainty as literal data."""
        def uncertain():
            return (overflow if conversion_witness(item) else computed,)
        if item is unknown:
            return uncertain()
        if operation == "format" and unresolved_text(item):
            return (item,)
        if operation == "quote" and not isinstance(item, str):
            return uncertain()
        if isinstance(item, Instance):
            names = ("__format__",) if operation == "format" else ("__repr__",) if operation in {"repr", "ascii"} else ("__str__", "__repr__")
            callbacks = ()
            for name in names:
                callbacks = member(item, name, scope, special=True)
                if callbacks:
                    break
            if not callbacks:
                return text_conversion(item, "str", scope, seen=seen) if operation == "format" and not spec else () if operation == "format" else uncertain()
            outputs = choices(output for callback in callbacks for output in invoke(callback, scope, [(spec,)] if operation == "format" else (), evaluated=True))
            return choices((output.encode("ascii", "backslashreplace").decode("ascii") if operation == "ascii" else output)
                           if isinstance(output, str) else computed for output in outputs if isinstance(output, str) or output is unknown)
        if operation == "format" and (spec is unknown or unresolved_text(spec)):
            return uncertain()
        if operation == "format" and not isinstance(item, (str, bytes, int, float, complex, bool, type(None))):
            return () if spec else text_conversion(item, "str", scope, seen=seen)
        if isinstance(item, (Sequence, Keywords, list, tuple)):
            if id(item) in seen:
                return ("...",)
            descendants = (*seen, id(item))
            try:
                literal = data_literal(item)
            except InexactDataLoop:
                literal = unknown
            if literal is not unknown:
                try:
                    return ({"str": str, "repr": repr, "ascii": ascii}[operation](literal),)
                except (ValueError, TypeError, OverflowError):
                    return ()
            child_operation = "ascii" if operation == "ascii" else "repr"
            def rendered_row(row, opening, closing):
                rendered = (opening,)
                for index, child in enumerate(row):
                    texts = text_conversion(child, child_operation, scope, seen=descendants)
                    rendered = choices(left + (", " if index else "") + right for left in rendered for right in texts)
                suffix = "," if opening == "(" and len(row) == 1 else ""
                return tuple(text + suffix + closing for text in rendered)
            if isinstance(item, Keywords):
                rendered = ("{",)
                for index, (key, assigned) in enumerate(item.items.items()):
                    keys = text_conversion(key, child_operation, scope, seen=descendants)
                    values = choices(text for child in assigned for text in text_conversion(child, child_operation, scope, seen=descendants))
                    rendered = choices(left + (", " if index else "") + key_text + ": " + child for left in rendered for key_text in keys for child in values)
                return choices(text + "}" for text in rendered)
            opening, closing = ("(", ")") if isinstance(item, tuple) else ("{", "}") if isinstance(item, Sequence) and item.unique else ("[", "]")
            rows = sequence_rows(item) if isinstance(item, Sequence) else (item,)
            return choices(text for row in rows for text in rendered_row(row, opening, closing))
        if item is unknown:
            return uncertain()
        try:
            literal = item if isinstance(item, str) else data_literal(item)
        except InexactDataLoop:
            return uncertain()
        try:
            return (format(literal, spec) if operation == "format" else {"str": str, "repr": repr, "ascii": ascii, "quote": shlex.quote}[operation](literal),)
        except (ValueError, TypeError, OverflowError):
            return ()

    def conversion_witness(item, seen=()):
        if id(item) in seen:
            return False
        if isinstance(item, str):
            return executable_operand(item)
        descendants = (*seen, id(item))
        if isinstance(item, Keywords):
            return any(conversion_witness(key, descendants) or any(conversion_witness(child, descendants) for child in assigned) for key, assigned in item.items.items())
        if isinstance(item, (Sequence, list, tuple)):
            return executable_value(item) or any(conversion_witness(child, descendants) for child in (sequence_members(item) if isinstance(item, Sequence) else item))
        return False

    class InvalidTextConversion(ValueError):
        pass

    def conversion_operand(item, scope):
        if type(item) in (str, bytes, int, float, complex, bool, type(None)):
            return item
        class Operand:
            def render(self, operation, spec=""):
                texts = text_conversion(item, operation, scope, spec)
                if not texts:
                    raise InvalidTextConversion("invalid modeled conversion")
                return texts[0] if len(texts) == 1 else overflow if any(executable_operand(text) for text in texts) else computed
            def numeric(self, operation, expected):
                assigned = data_call_protocol(operation, [(item,)], {}, scope)
                if not assigned:
                    raise InvalidTextConversion("unsupported numeric conversion")
                if len(assigned) != 1 or type(assigned[0]) is not expected:
                    raise ValueError("uncertain numeric conversion")
                return assigned[0]
            def __int__(self):
                return self.numeric("int", int)
            def __index__(self):
                return self.numeric("index", int)
            def __float__(self):
                return self.numeric("float", float)
            def __str__(self):
                return self.render("str")
            def __repr__(self):
                return self.render("repr")
            def __format__(self, spec):
                return self.render("format", spec)
        return Operand()

    text_conversions = {name: operation for operation in ("str", "repr", "ascii") for name in (operation, "builtins." + operation)}
    text_conversions["shlex.quote"] = "quote"

    protocol_data_calls = {name for operation in ("len", "bool", "int", "float", "complex", "hash", "dir", "sorted", "min", "max", "any", "all", "sum", "bytes", "range", "set", "frozenset", "dict")
                           for name in (operation, "builtins." + operation)} | {"pprint.pprint", "pprint.pformat", "json.dumps", "json.dump", "json.loads"}

    def data_call_protocol(name, arguments, keywords, scope):
        """Data consumers can execute class protocols and explicit callbacks."""
        operation = name.removeprefix("builtins.").removeprefix("operator.")
        def alternatives(actions):
            return evaluation_alternatives(tuple(actions), scope, lambda action: action(scope))

        def special(item, names, local, args=()):
            if not isinstance(item, Instance):
                return ()
            for method in names:
                callbacks = member(item, method, local, special=True)
                if callbacks:
                    return alternatives(lambda target, callback=callback: invoke(callback, target, args, evaluated=True) for callback in callbacks)
            return ()

        def truth(item, local):
            if isinstance(item, Instance):
                outputs = special(item, ("__bool__", "__len__"), local)
                return any(bool(output) for output in outputs if type(output) in (bool, int)) if outputs else True
            try:
                return bool(data_literal(item))
            except InexactDataLoop:
                return False

        if operation == "complex" and (len(arguments) > 1 or keywords):
            if len(arguments) > 2 or set(keywords) - {"real", "imag"} or (arguments and "real" in keywords) or (len(arguments) > 1 and "imag" in keywords):
                return (unknown,)
            real = arguments[0] if arguments else keywords.get("real", (0,))
            imag = arguments[1] if len(arguments) > 1 else keywords.get("imag", (0,))
            returned = data_call_protocol("complex", [real], {}, scope)
            if not any(type(item) in (int, float, complex, bool) or item is unknown for item in returned):
                return ()
            data_call_protocol("float", [imag], {}, scope)
            return (unknown,)
        if operation in {"len", "bool", "int", "float", "complex", "hash", "dir", "index"}:
            if len(arguments) != 1 or keywords:
                return (unknown,)
            protocols = {"len": ("__len__",), "bool": ("__bool__", "__len__"), "int": ("__int__", "__index__"),
                         "float": ("__float__", "__index__"), "complex": ("__complex__", "__float__", "__index__"),
                         "hash": ("__hash__",), "dir": ("__dir__",), "index": ("__index__",)}
            def convert(item, local):
                if item is unknown or unresolved_text(item):
                    return (unknown,)
                if isinstance(item, Instance):
                    return special(item, protocols[operation], local)
                try:
                    literal = data_literal(item)
                    if operation == 'hash' and process_salted(literal):
                        return (unknown,)
                    return (int.__index__(literal) if operation == "index" else getattr(builtins, operation)(literal),)
                except (InexactDataLoop, TypeError, ValueError, OverflowError):
                    return (unknown,)
            return alternatives(lambda local, item=item: convert(item, local) for item in arguments[0])
        if operation in {"pprint.pprint", "pprint.pformat"}:
            if not arguments:
                return (unknown,)
            positions = ("stream", "indent", "width", "depth") if operation == "pprint.pprint" else ("indent", "width", "depth")
            if len(arguments) > len(positions) + 1 or any(key in keywords for key in positions[:len(arguments) - 1]):
                return (unknown,)
            keywords = {**dict(zip(positions, arguments[1:])), **keywords}
            allowed = {"indent", "width", "depth", "compact", "sort_dicts", "underscore_numbers"}
            if set(keywords) - (allowed | ({"stream"} if operation == "pprint.pprint" else set())):
                return (unknown,)
            class InexactPrettyTree(Exception):
                pass
            def render(item, local):
                class Representation:
                    def __init__(self, operand):
                        self.operand = operand
                    def __repr__(self):
                        texts = special(self.operand, ("__repr__",), local)
                        if len(texts) != 1:
                            uncertain_protocol_effects((self.operand,), local, tuple((callback, ()) for callback in member(self.operand, '__repr__', local, special=True)))
                            return overflow if any(conversion_witness(text) for text in texts) else computed
                        return texts[0] if isinstance(texts[0], str) else computed
                def bounded_rows(parts):
                    rows = [()]
                    for part in parts:
                        if len(rows) * len(part) > 8:
                            raise InexactPrettyTree()
                        rows = [(*row, child) for row in rows for child in part]
                    return rows
                def trees(operand, seen=()):
                    if id(operand) in seen:
                        return (computed,)
                    descendants = (*seen, id(operand))
                    if isinstance(operand, Instance):
                        return (Representation(operand),)
                    if isinstance(operand, Keywords):
                        keys = list(operand.items)
                        entries = [tuple(child for assigned in operand.items[key] for child in trees(assigned, descendants)) for key in keys]
                        return tuple(dict(zip(keys, row)) for row in bounded_rows(entries))
                    if isinstance(operand, (Sequence, list, tuple)):
                        rows = sequence_rows(operand) if isinstance(operand, Sequence) else (operand,)
                        rendered = []
                        for entries in rows:
                            for row in bounded_rows([trees(child, descendants) for child in entries]):
                                if len(rendered) == 8:
                                    raise InexactPrettyTree()
                                rendered.append(tuple(row) if isinstance(operand, tuple) else list(row))
                        return tuple(rendered)
                    return (computed if operand is unknown else operand,)
                options = {}
                for key, assigned in keywords.items():
                    if key == "stream":
                        continue
                    if len(assigned) != 1:
                        continue
                    try:
                        options[key] = data_literal(assigned[0])
                    except InexactDataLoop:
                        pass
                def display(tree, target):
                    def to_stream(stream):
                        if stream is None:
                            return io.StringIO()
                        class Destination:
                            @property
                            def write(self):
                                callbacks = member(stream, "write", target)
                                if not callbacks:
                                    raise AttributeError("stream has no write")
                                return lambda text: alternatives(lambda local, callback=callback: invoke(callback, local, [(text,)], evaluated=True)
                                                                 for callback in callbacks)
                        return Destination()
                    if operation == "pprint.pprint":
                        streams = choices(destination for stream in keywords.get("stream", (None,))
                                          for destination in (patched_attributes.get("sys.stdout", (None,)) if stream is None else (stream,)))
                        def pretty(stream, local):
                            try:
                                pprint.pprint(tree, stream=to_stream(stream), **options)
                            except (AttributeError, TypeError, ValueError, OverflowError):
                                pass
                            return (None,)
                        return alternatives(lambda local, stream=stream: pretty(stream, local) for stream in streams)
                    try:
                        return (pprint.pformat(tree, **options),)
                    except (TypeError, ValueError, OverflowError):
                        return (unknown,)
                try:
                    concrete = trees(item)
                except InexactPrettyTree:
                    # A widened tree cannot establish how many repr visits occur.
                    # Keep the original state and each relevant callback branch.
                    instances, pending, seen = [], [item], set()
                    while pending:
                        operand = pending.pop()
                        if id(operand) in seen:
                            continue
                        seen.add(id(operand))
                        if isinstance(operand, Instance):
                            instances.append(operand)
                        elif isinstance(operand, Keywords):
                            pending.extend(child for assigned in operand.items.values() for child in assigned)
                        elif isinstance(operand, (Sequence, list, tuple)):
                            pending.extend(sequence_members(operand) if isinstance(operand, Sequence) else operand)
                    uncertain_protocol_effects((item,), local, tuple((callback, ()) for instance in instances
                                                for callback in member(instance, '__repr__', local, special=True)))
                    return display(computed, local) if operation == "pprint.pprint" else (computed,)
                return alternatives(lambda target, tree=tree: display(tree, target) for tree in concrete)
            return alternatives(lambda local, item=item: render(item, local) for item in arguments[0]) or (unknown,)
        def protocol_leaves(operands, *, json_native=False, skip_empty=False, local=None):
            pending, visited = list(operands), set()
            while pending:
                item = pending.pop()
                if id(item) in visited:
                    continue
                visited.add(id(item))
                if json_native and isinstance(item, Instance) and json_native_kind(item.kind):
                    native = json_native_kind(item.kind)
                    protocols = json_native_protocols(item, scope if local is None else local, skip_empty=skip_empty)
                    if protocols:
                        yield item
                    elif native in {"list", "tuple", "dict"}:
                        pending.extend(item.json_payload)
                elif isinstance(item, Instance):
                    yield item
                elif isinstance(item, Keywords):
                    pending.extend(child for assigned in item.items.values() for child in assigned)
                    if not json_native:
                        pending.extend(item.items)
                elif isinstance(item, Sequence):
                    if json_native and item.unique:
                        yield item
                    else:
                        pending.extend(sequence_members(item))
                elif isinstance(item, (list, tuple)):
                    pending.extend(item)
                elif item is unknown or isinstance(item, (Generator, Function, Bound, Partial, ast.alias, ast.ClassDef)) or json_native and type(item) in (bytes, complex):
                    yield item

        if operation in {'json.dumps', 'json.dump', 'json.loads'}:
            parameters = ('obj','fp') if operation == 'json.dump' else ('s',) if operation == 'json.loads' else ('obj',)
            arguments, keywords = list(arguments), dict(keywords)
            if len(arguments) > len(parameters):
                return (unknown,)
            for index, parameter in enumerate(parameters):
                if index < len(arguments):
                    if parameter in keywords:
                        return (unknown,)
                elif parameter in keywords:
                    arguments.append(keywords.pop(parameter))
                else:
                    return (unknown,)
            if keywords.get('cls') == (None,):
                keywords.pop('cls')
            allowed = ({'object_hook','object_pairs_hook','parse_float','parse_int','parse_constant','strict'}
                       if operation == 'json.loads' else
                       {'skipkeys','ensure_ascii','check_circular','allow_nan','indent','separators','default','sort_keys'})
            if 'cls' not in keywords and set(keywords) - allowed:
                return (unknown,)
            if operation != 'json.loads' and 'cls' not in keywords and keywords.get('indent') and all(
                    type(indent) in (float, complex, bytes) or isinstance(indent, (Sequence, Keywords, list, tuple))
                    for indent in keywords['indent']):
                return (unknown,)
            operands = tuple(item for assigned in arguments for item in assigned)
            callbacks = []
            if operation == 'json.loads':
                # CPython's parser selects which explicit callbacks are reached.
                source = arguments[0]
                if len(source) == 1 and isinstance(source[0], (str, bytes)) and not unresolved_text(source[0]) and 'cls' not in keywords:
                    handlers = {}
                    def adapt(item):
                        if isinstance(item, dict):
                            return Keywords({key:(adapt(child),) for key, child in item.items()})
                        if isinstance(item, list):
                            return Sequence(([adapt(child) for child in item],))
                        return item
                    for name in ('object_hook', 'object_pairs_hook', 'parse_float', 'parse_int', 'parse_constant'):
                        if name in keywords and keywords[name] != (None,):
                            def handler(item, assigned=keywords[name]):
                                values = alternatives(lambda target, callback=callback: invoke(callback, target, [(adapt(item),)], evaluated=True) for callback in assigned)
                                return values[0] if len(values) == 1 else overflow if any(conversion_witness(value) for value in values) else unknown
                            handlers[name] = handler
                    try:
                        return (adapt(json.loads(source[0], **handlers)),)
                    except (TypeError, ValueError, OverflowError):
                        return (unknown,)
                callbacks.extend((callback, [(unknown,)]) for name, assigned in keywords.items() if name in {'object_hook','object_pairs_hook','parse_float','parse_int','parse_constant'} for callback in assigned if callback is not None)
            else:
                # Primitive JSON without callbacks is inert. In particular, do
                # not expand a factored source DAG just to learn its layout.
                defaults = tuple(callback for callback in keywords.get('default', ()) if callback is not None)
                if operation == 'json.dump' and len(arguments) > 1:
                    callbacks.extend((callback, [(computed,)]) for stream in arguments[1] for callback in member(stream, 'write', scope))
            for encoder in keywords.get('cls', ()):
                for instance in invoke(encoder, scope, evaluated=True):
                    callbacks.extend((callback, [arguments[0]]) for method in ('decode','raw_decode') if operation == 'json.loads'
                                     for callback in member(instance, method, scope))
                    if operation != 'json.loads':
                        callbacks.extend((callback, [arguments[0]]) for method in ('encode','iterencode')
                                         for callback in member(instance, method, scope))
                        defaults = (*defaults, *member(instance, 'default', scope))
            if callbacks:
                uncertain_protocol_effects(operands, scope, callbacks)
            if operation != 'json.loads':
                def encode_defaults(local, skip_empty):
                    def leaves(values):
                        return protocol_leaves(values, json_native=True, skip_empty=skip_empty, local=local)
                    def default_callbacks(values):
                        callbacks = []
                        for item in leaves(values):
                            native = json_native_kind(item.kind) if isinstance(item, Instance) else None
                            if native in {"list", "tuple", "dict"}:
                                callbacks.extend((Bound(ast.alias(name="json.iterate"), callback), ())
                                                 for callback in json_native_protocols(item, local, skip_empty=skip_empty))
                            else:
                                callbacks.extend((callback, [(item,)]) for callback in defaults)
                        return tuple(callbacks)
                    initial = default_callbacks(arguments[0])
                    if len(arguments[0]) == 1 and isinstance(arguments[0][0], Instance) and len(initial) == 1:
                        callback, args = initial[0]
                        outputs = invoke(callback, local, args, evaluated=True)
                        protocol_return_effects(operands, local, default_callbacks, outputs, leaves)
                    elif initial:
                        protocol_return_effects(operands, local, default_callbacks, receivers_for=leaves)
                    return (unknown,)
                # Indented dumps can select either encoder across supported
                # Python versions. Keep their callback states as alternatives.
                modes = ((True,) if operation == 'json.dump' else (False, True)
                         if any(indent is not None for indent in keywords.get('indent', ())) else (False,))
                return alternatives(lambda local, mode=mode: encode_defaults(local, mode) for mode in modes)
            return (unknown,)
        if operation in {'any','all','sum','bytes','range','set','frozenset','dict'}:
            if operation == 'bytes' and 'source' in keywords:
                if arguments:
                    return (unknown,)
                arguments, keywords = [keywords['source']], {key:assigned for key,assigned in keywords.items() if key != 'source'}
            if operation in {'any','all'} and (len(arguments) != 1 or keywords):
                return (unknown,)
            if operation == 'sum' and (not 1 <= len(arguments) <= 2 or set(keywords) - {'start'} or len(arguments) == 2 and 'start' in keywords):
                return (unknown,)
            if operation == 'range' and (not 1 <= len(arguments) <= 3 or keywords):
                return (unknown,)
            if operation in {'set','frozenset'} and (len(arguments) > 1 or keywords):
                return (unknown,)
            if operation == 'dict' and len(arguments) > 1:
                return (unknown,)
            if operation == 'bytes' and (len(arguments) > 3 or set(keywords) - {'encoding','errors'} or len(arguments) > 1 and 'encoding' in keywords or len(arguments) > 2 and 'errors' in keywords):
                return (unknown,)
            operands = tuple(item for assigned in (*arguments, *keywords.values()) for item in assigned)
            leaves = tuple(protocol_leaves(operands))
            methods = {'any': ('__bool__','__len__'), 'all': ('__bool__','__len__'),
                       'sum': ('__add__','__radd__'), 'bytes': ('__bytes__','__index__','__iter__','__next__'),
                       'range': ('__index__',), 'set': ('__hash__','__eq__','__iter__','__next__'),
                       'frozenset': ('__hash__','__eq__','__iter__','__next__'),
                       'dict': ('__hash__','__eq__','__iter__','__next__','keys','__getitem__')}
            callbacks = []
            for item in leaves:
                for method in methods[operation]:
                    present = member(item, method, scope, special=method != 'keys')
                    callbacks.extend((callback, [(unknown,)] if method in {'__add__','__radd__','__eq__','__getitem__'} else ()) for callback in present)
                    if present and (operation in {'any','all'} or operation == 'bytes' and method in {'__bytes__','__index__'}):
                        break
            if callbacks:
                returned = uncertain_protocol_effects(operands, scope, callbacks)
                if operation not in {'dict','set','frozenset'}:
                    return (overflow if any(conversion_witness(item) for item in returned) else unknown,)
            try:
                positional = [data_literal(assigned[0]) if len(assigned) == 1 else (_ for _ in ()).throw(InexactDataLoop()) for assigned in arguments]
                named = {key:data_literal(assigned[0]) if len(assigned) == 1 else (_ for _ in ()).throw(InexactDataLoop()) for key, assigned in keywords.items()}
                result = getattr(builtins, operation)(*positional, **named)
                if isinstance(result, range):
                    if len(result) > 10000:
                        return (unknown,)
                    result = tuple(result)
                if isinstance(result, dict):
                    return (Keywords({key:(item,) for key,item in result.items()}),)
                if isinstance(result, (list,set,frozenset)):
                    return (Sequence((list(result),), unique=isinstance(result,(set,frozenset))),)
                return (result,)
            except (InexactDataLoop, TypeError, ValueError, OverflowError):
                if operation == 'dict':
                    mapping = {}
                    for operand in arguments[0] if arguments else ():
                        if isinstance(operand, Keywords):
                            update_mapping(mapping, operand.items)
                        else:
                            for entry in iterate_values((operand,), scope):
                                if isinstance(entry, (list, tuple)) and len(entry) == 2:
                                    put_mapping(mapping, entry[0] if isinstance(entry[0], (str, int)) else unknown, (entry[1],))
                                else:
                                    put_mapping(mapping, unknown, (entry,))
                    update_mapping(mapping, keywords)
                    return (Keywords(mapping),)
                if operation in {'set','frozenset'} and arguments:
                    rows = tuple(row for source in arguments[0] for row in
                                 (source.state["rows"] if isinstance(source, Sequence) and source.unique else iterable_rows((source,), scope)))
                    return (Sequence(rows, unique=True),)
                return (unknown,)
        if operation in {"sorted", "min", "max"}:
            if not arguments:
                return (unknown,)
            allowed = {"key", "reverse"} if operation == "sorted" else {"key", "default"}
            if set(keywords) - allowed or operation == "sorted" and len(arguments) != 1 or operation != "sorted" and len(arguments) > 1 and "default" in keywords:
                return (unknown,)
            rows = iterable_rows(arguments[0], scope) if len(arguments) == 1 else combinations(arguments)
            def order(row, local):
                uncertain = False
                def compare(left, right, method):
                    nonlocal uncertain
                    callbacks = member(left, method, local, special=True) if isinstance(left, Instance) else ()
                    reflected = "__gt__" if method == "__lt__" else "__lt__"
                    if callbacks:
                        returned = alternatives(lambda target, callback=callback: invoke(callback, target, [(right,)], evaluated=True) for callback in callbacks)
                        uncertain |= len(returned) != 1 or any(item is unknown or unresolved_text(item) for item in returned)
                        definite = tuple(item for item in returned if item is not NotImplemented)
                        if not any(item is NotImplemented for item in returned):
                            return any(alternatives(lambda target, item=item: (truth(item, target),) for item in definite))
                    callbacks = member(right, reflected, local, special=True) if isinstance(right, Instance) else ()
                    if callbacks:
                        returned = alternatives(lambda target, callback=callback: invoke(callback, target, [(left,)], evaluated=True) for callback in callbacks)
                        uncertain |= len(returned) != 1 or any(item is unknown or unresolved_text(item) for item in returned)
                        return any(truth(item, local) for item in returned)
                    try:
                        return data_literal(left) < data_literal(right) if method == "__lt__" else data_literal(left) > data_literal(right)
                    except (InexactDataLoop, TypeError, ValueError):
                        uncertain = True
                        return False
                reverse_values = []
                if operation == 'sorted':
                    for flag in keywords.get('reverse', (False,)):
                        if isinstance(flag, Instance):
                            assigned = special(flag, ('__bool__','__len__'), local)
                            reverse_values.extend(bool(item) if type(item) in (bool,int) else unknown for item in assigned)
                            if not assigned:
                                reverse_values.append(True)
                        else:
                            try:
                                reverse_values.append(bool(data_literal(flag)))
                            except InexactDataLoop:
                                reverse_values.append(unknown)
                    uncertain |= any(flag is unknown for flag in reverse_values) or len(set(reverse_values)) != 1
                reversed_order = any(flag is True for flag in reverse_values)
                class Ordered:
                    def __init__(self, item):
                        self.item = item
                        self.keys = alternatives(lambda target, callback=callback: (item,) if callback is None else invoke(callback, target, [(item,)], evaluated=True)
                                                 for callback in keywords.get("key", (None,))) or (unknown,)
                        nonlocal uncertain
                        uncertain |= len(self.keys) != 1 or any(key is unknown or unresolved_text(key) for key in self.keys)
                    def __lt__(self, other):
                        return any(alternatives(lambda target, left=left, right=right: (compare(left, right, "__lt__"),)
                                                for left in self.keys for right in other.keys))
                    def __gt__(self, other):
                        return any(alternatives(lambda target, left=left, right=right: (compare(left, right, "__gt__"),)
                                                for left in self.keys for right in other.keys))
                wrapped = [Ordered(item) for item in row]
                if operation == "sorted":
                    result = sorted(wrapped, reverse=reversed_order)
                    rows = ([entry.item for entry in result],)
                    if uncertain:
                        rows = (*rows, list(row), list(reversed(row)), list(reversed(rows[0])))
                    return (Sequence(rows), *((overflow,) if uncertain and conversion_witness(row) else ()))
                if not wrapped:
                    return keywords.get("default", (unknown,))
                selected = (min if operation == "min" else max)(wrapped).item
                return choices(row) if uncertain else (selected,)
            result = alternatives(lambda local, row=row: order(row, local) for row in rows)
            if len(arguments) == 1:
                exhaust_generators(arguments[0])
            return result or (unknown,)
        return (unknown,)

    def value(node, scope):
        if isinstance(node, ast.FormattedValue) or isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            cached = expression_values.get((scope, id(node)))
            if cached is not None:
                return cached
        if isinstance(node, ast.Name):
            if node.id == "NotImplemented" and scope.get(node.id) is None:
                return (NotImplemented,)
            if scope.owner(node.id) is None and "builtins." + node.id in patched_attributes:
                return patched_attributes["builtins." + node.id]
            if scope.owner(node.id) is None and "builtins." + node.id in exception_types:
                return (ExceptionValue("builtins." + node.id),)
            return tuple(ExceptionValue(item.name) if isinstance(item, ast.alias) and item.name in exception_types else item
                         for item in scope.get(node.id, (ast.alias(name=node.id),) if node.id in pure_data_calls | descriptor_names else (unknown,)))
        if isinstance(node, ast.Await):
            cached = expression_values.get((scope, id(node)))
            if cached is not None:
                return cached
            result = consume_deferred(value(node.value, scope), scope)
            expression_values[scope, id(node)] = result
            return result
        if isinstance(node, ast.Constant):
            return (node.value,)
        if (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub))
                and isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float, complex)):
            return (ast.literal_eval(node),)
        if isinstance(node, ast.Dict):
            items = {}
            for key, expression in zip(node.keys, node.values):
                if key is None:
                    for mapping in value(expression, scope):
                        if isinstance(mapping, Keywords):
                            update_mapping(items, mapping.items)
                else:
                    for literal in value(key, scope):
                        put_mapping(items, literal if isinstance(literal, (str, int)) else unknown,
                                    value(expression, scope))
            return (Keywords(items),)
        if isinstance(node, ast.Subscript):
            resolved = []
            for assigned in value(node.value, scope):
                for container in sequence_rows(assigned):
                    for key in value(node.slice, scope):
                        if isinstance(container, Keywords):
                            resolved.extend(container.items.get(key, (unknown,)) if isinstance(key, (str, int)) else choices(item for assigned in container.items.values() for item in assigned))
                            resolved.extend(container.items.get(unknown, ()))
                        elif isinstance(container, (list, tuple)) and isinstance(key, int) and -len(container) <= key < len(container):
                            resolved.append(container[key])
                        elif isinstance(container, (list, tuple)) and (key is unknown or unresolved_text(key)):
                            resolved.extend(container)
            return choices(resolved) or (unknown,)
        if isinstance(node, ast.Attribute):
            cached = expression_values.get((scope, id(node)))
            if cached is not None:
                return cached
            targets = {parent + "." + node.attr for parent in function_names(node.value, scope)}
            replaced = [item for target in targets if target in patched_attributes for item in patched_attributes[target]]
            resolved = [*replaced, *(ExceptionValue(name) if name in exception_types else ast.alias(name=name)
                                   for name in function_names(node, scope)
                                   if name in launches | external_mutations | pure_data_calls | descriptor_names | mock_types | exception_types | async_names | {"unittest.mock.DEFAULT"})]
            if "unittest.mock.patch.stopall" in function_names(node, scope):
                resolved.append(Bound(ast.alias(name="patch.stopall"), None))
            for owner in value(node.value, scope):
                if isinstance(owner, Deferred) and node.attr == "start":
                    resolved.append(owner.callback)
                if isinstance(owner, Registry) and node.attr == "func":
                    resolved.extend(owner.handlers)
                if isinstance(owner, (Sequence, Keywords)) and node.attr in mutation_methods:
                    resolved.append(Bound(ast.alias(name="methodcaller." + node.attr), owner))
                if isinstance(owner, PatchContext) and node.attr in {"start", "stop"}:
                    resolved.append(Bound(ast.alias(name="patch." + node.attr), owner))
                if is_value_iterator(owner) and node.attr in {"__next__", "__iter__"}:
                    resolved.append(Bound(ast.alias(name="iterator." + node.attr), owner))
                resolved.extend(member(owner, node.attr, scope))
            result = choices(resolved) or (unknown,)
            expression_values[scope, id(node)] = result
            return result
        if isinstance(node, ast.Lambda):
            return (Function(node, scope),)
        if isinstance(node, ast.GeneratorExp):
            cached = expression_values.get((scope, id(node)))
            if cached is None:
                # The eager outer iterator is evaluated once in this evaluation.
                scan(node.generators[0].iter, scope)
                cached = (Generator(node, scope, arguments=(value(node.generators[0].iter, scope),)),)
                expression_values[scope, id(node)] = cached
            return cached
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)):
            local = comprehension(node, scope)
            if isinstance(node, ast.DictComp):
                items = {}
                if local is not None:
                    for key in value(node.key, local):
                        put_mapping(items, key if isinstance(key, (str, int)) else unknown, value(node.value, local))
                return (Keywords(items),)
            return (Sequence((list(value(node.elt, local)) if local is not None else [],), unique=isinstance(node, ast.SetComp)),)
        if isinstance(node, ast.JoinedStr):
            rendered = ("",)
            for part in node.values:
                rendered = choices(first + (second if isinstance(second, str) else computed) for first in rendered for second in value(part, scope))
            return rendered
        if isinstance(node, ast.FormattedValue):
            specs = value(node.format_spec, scope) if node.format_spec else ("",)
            operation = {ord("r"): "repr", ord("s"): "str", ord("a"): "ascii"}.get(node.conversion)
            alternatives = [(item, spec) for item in value(node.value, scope) for spec in specs]
            def render(alternative):
                item, spec = alternative
                converted = text_conversion(item, operation, scope) if operation else (item,)
                return choices(text for literal in converted for text in
                    (text_conversion(literal, "format", scope, spec) if isinstance(spec, str)
                     else (overflow if conversion_witness(literal) else computed,)))
            result = evaluation_alternatives(alternatives, scope, render)
            expression_values[scope, id(node)] = result
            return result
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            parts = expanded_arguments(node.elts, scope)
            if isinstance(node, ast.List) and any(len(part) > 1 for part in parts):
                rows, run = ([],), []
                for part in parts:
                    if len(part) == 1:
                        run.append(part[0])
                        continue
                    if run:
                        rows = (RowConcat(rows, (run,)),)
                        run = []
                    rows = (RowConcat(rows, ([item] for item in part)),)
                if run:
                    rows = (RowConcat(rows, (run,)),)
                return (Sequence(rows),)
            rows = combinations(parts)
            return choices(tuple(row) for row in rows) if isinstance(node, ast.Tuple) else (Sequence(rows, unique=isinstance(node, ast.Set)),)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            assigned = add(value(node.left, scope), value(node.right, scope))
            rows = [row for row in assigned if isinstance(row, (list, RowConcat))]
            return choices((*([Sequence(rows)] if rows else []), *(row for row in assigned if not isinstance(row, (list, RowConcat)))))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            alternatives = []
            for template in value(node.left, scope):
                for item in value(node.right, scope):
                    if isinstance(item, Keywords):
                        alternatives.extend((template, Keywords(dict(zip(item.items, [(child,) for child in row]))))
                                            for row in combinations(list(item.items.values())))
                    else:
                        alternatives.append((template, item))
            def render(alternative):
                template, item = alternative
                if template is unknown or unresolved_text(template):
                    return unknown_template_effects((item,), scope, percent=True, template=template)
                if not isinstance(template, str):
                    return (unknown,)
                if isinstance(item, tuple):
                    operands = tuple(conversion_operand(child, scope) for child in item)
                elif isinstance(item, Keywords):
                    operands = {key: conversion_operand(assigned[0], scope) for key, assigned in item.items.items()}
                else:
                    operands = conversion_operand(item, scope)
                try:
                    return (template % operands,)
                except InvalidTextConversion:
                    return ()
                except (TypeError, ValueError, KeyError, OverflowError):
                    return format_failure(template, (item,), percent=True)
            result = evaluation_alternatives(alternatives, scope, render)
            expression_values[scope, id(node)] = result
            return result
        if isinstance(node, ast.IfExp):
            return choices((*value(node.body, scope), *value(node.orelse, scope)))
        if isinstance(node, ast.Call):
            cached = expression_values.get((scope, id(node)))
            if cached is not None:
                return cached
            names = function_names(node.func, scope)
            if names & protocol_data_calls:
                arguments, keywords = expanded_arguments(node.args, scope), call_keywords(node.keywords, scope)
                functions = value(node.func, scope)
                result = evaluation_alternatives(tuple(functions), scope,
                                                 lambda function: invoke(function, scope, arguments, keywords, evaluated=True)) or (unknown,)
                expression_values[scope, id(node)] = result
                return result
            transformed = string_transform(node, scope, names)
            if transformed is not None:
                expression_values[scope, id(node)] = transformed
                return transformed
            mutated = mutate(node, scope)
            if mutated is not None:
                expression_values[scope, id(node)] = mutated
                return mutated
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"keys", "values", "items", "get"}:
                mappings = [owner for owner in value(node.func.value, scope) if isinstance(owner, Keywords)]
                if mappings:
                    result = []
                    for owner in mappings:
                        if node.func.attr == "get":
                            keys = value(node.args[0], scope) if node.args else (unknown,)
                            for key in keys:
                                default = value(node.args[1], scope) if len(node.args) > 1 else (None,)
                                result.extend(owner.items.get(key, default) if isinstance(key, (str, int)) else choices((*default, *(item for assigned in owner.items.values() for item in assigned))))
                                result.extend(owner.items.get(unknown, ()))
                        else:
                            items = list(owner.items) if node.func.attr == "keys" else [item if node.func.attr == "values" else (key, item) for key, assigned in owner.items.items() for item in assigned]
                            result.append(Sequence((items,)))
                    return choices(result) or (unknown,)
            mock_constructors = {function.name for function in value(node.func, scope)
                                 if isinstance(function, ast.alias) and function.name in mock_types}
            if mock_constructors:
                result = tuple(make_mock(name, call_keywords(node.keywords, scope)) for name in mock_constructors)
                expression_values[scope, id(node)] = result
                return result
            if names & exception_types or any(isinstance(item, ExceptionValue) for item in value(node.func, scope)):
                return tuple(ExceptionValue(name) for name in names & exception_types) or value(node.func, scope)
            if names & loop_factories:
                result = (ast.alias(name="asyncio.AbstractEventLoop"),)
                expression_values[scope, id(node)] = result
                return result
            if names & async_consumers:
                arguments, keywords = expanded_arguments(node.args, scope), call_keywords(node.keywords, scope)
                result = choices(item for name in names & async_consumers
                                 for item in async_result(name, arguments, keywords, scope))
                expression_values[scope, id(node)] = result
                return result
            if names & {"iter", "builtins.iter", "itertools.cycle"} and len(node.args) == 1 and not node.keywords:
                result = choices(item if is_value_iterator(item) and name != "itertools.cycle" else make_iterator((item,), name == "itertools.cycle")
                                 for item in value(node.args[0], scope) for name in names & {"iter", "builtins.iter", "itertools.cycle"})
                expression_values[scope, id(node)] = result
                return result
            if names & {"next", "builtins.next"} and node.args:
                operands = value(node.args[0], scope)
                if any(is_value_iterator(item) for item in operands):
                    result = choices(value for item in operands for value in (consume_iterator(item, scope) if is_value_iterator(item) else iterate_values((item,), scope)))
                    result = result or (value(node.args[1], scope) if len(node.args) > 1 else (unknown,))
                    expression_values[scope, id(node)] = result
                    return result
            if names & {"unittest.mock.patch", "unittest.mock.patch.object", "unittest.mock.patch.multiple"}:
                arguments = expanded_arguments(node.args, scope)
                keywords = call_keywords(node.keywords, scope)
                object_patch = "unittest.mock.patch.object" in names
                target = arguments[0] if arguments else keywords.get("target", ())
                if "unittest.mock.patch.multiple" in names:
                    children = []
                    options = {name: assigned for name, assigned in keywords.items() if name in {"new_callable", "spec", "spec_set", "create", "autospec"}}
                    for name, assigned in keywords.items():
                        if name in options or name == "target":
                            continue
                        replacement, inject = patch_replacement(assigned, options, scope)
                        children.extend(object_patch_context(parent, name, replacement, inject, options.get("create") == (True,)) for parent in target if isinstance(name, str))
                    result = (PatchContext(None, (), children=tuple(children)),)
                    expression_values[scope, id(node)] = result
                    return result
                attribute = arguments[1] if len(arguments) > 1 else keywords.get("attribute", ())
                offset = 2 if object_patch else 1
                replacement = arguments[offset] if len(arguments) > offset else keywords.get("new", ())
                replacement, inject = patch_replacement(replacement, keywords, scope)
                # An unresolved replacement can still retain the original sink.
                if replacement:
                    result = (choices(object_patch_context(parent, attr, replacement, inject, keywords.get("create") == (True,)) for parent in target for attr in attribute)
                              if object_patch else choices(PatchContext(parent, replacement, inject) if isinstance(parent, str) else
                                                           PatchContext(computed, replacement, inject, attribute_owner=unknown) for parent in target))
                    expression_values[scope, id(node)] = result
                    return result
            if "compile" in names and node.args:
                return choices(CompiledCode(source) for source in value(node.args[0], scope) if isinstance(source, str)) or (unknown,)
            if names & {"contextlib.ExitStack", "contextlib.AsyncExitStack"}:
                result = choices(Instance(ast.alias(name=name)) for name in names)
                expression_values[scope, id(node)] = result
                return result
            if isinstance(node.func, ast.Attribute):
                stacks = [owner for owner in value(node.func.value, scope) if is_exit_stack(owner)]
                if stacks and node.func.attr in {"enter_context", "enter_async_context", "callback", "push", "close", "aclose"}:
                    results = []
                    for stack in stacks:
                        if node.func.attr in {"close", "aclose"}:
                            close_context(stack, scope)
                        elif node.args:
                            callbacks = []
                            if node.func.attr in {"enter_context", "enter_async_context"}:
                                entered, callbacks = enter_context(value(node.args[0], scope), scope, async_=node.func.attr == "enter_async_context")
                                results.extend(entered)
                            elif node.func.attr == "callback":
                                functions = value(node.args[0], scope)
                                results.extend(functions)
                                callbacks = [Partial(functions, expanded_arguments(node.args[1:], scope), call_keywords(node.keywords, scope))]
                            else:
                                for callback in value(node.args[0], scope):
                                    callbacks.append(Partial(member(callback, "__exit__") if isinstance(callback, Instance) else (callback,), [(unknown,)] * 3, {}))
                            observe(stack.attributes)
                            stack.attributes["_context_exits"] = (*stack.attributes.get("_context_exits", ()), *callbacks)
                    result = choices(results) or (None,)
                    expression_values[scope, id(node)] = result
                    return result
            descriptors = names & descriptor_names
            if descriptors:
                arguments = expanded_arguments(node.args, scope)
                keywords = call_keywords(node.keywords, scope)
                callbacks = [arguments[index] if len(arguments) > index else keywords.get(parameter, (None,))
                             for index, parameter in enumerate(("fget", "fset", "fdel"))]
                return tuple(Descriptor(function, name.rsplit(".", 1)[-1], callbacks[1], callbacks[2])
                             for name in descriptors for function in callbacks[0])
            if "dict" in names:
                mapping = {}
                for operand in value(node.args[0], scope) if node.args else ():
                    if isinstance(operand, Keywords):
                        update_mapping(mapping, operand.items)
                    else:
                        for entry in iterate_values((operand,), scope):
                            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                                key = entry[0] if isinstance(entry[0], (str, int)) else unknown
                                put_mapping(mapping, key, (entry[1],))
                            else:
                                put_mapping(mapping, unknown, (entry,))
                update_mapping(mapping, call_keywords(node.keywords, scope))
                return (Keywords(mapping),)
            if not node.args and names & {"list", "set", "frozenset", "tuple"}:
                return (() if "tuple" in names else Sequence(([],), unique=bool(names & {"set", "frozenset"})),)
            if "super" in names:
                parameters = [*active[-1].args.posonlyargs, *active[-1].args.args] if active else []
                kinds = value(node.args[0], scope) if node.args else (method_classes.get(id(active[-1])) if active else None,)
                receivers = value(node.args[1], scope) if len(node.args) > 1 else scope.get(parameters[0].arg, ()) if parameters else ()
                return choices(Super(kind, receiver) for kind in kinds for receiver in receivers if isinstance(kind, ast.ClassDef)) or (unknown,)
            if "enumerate" in names and node.args:
                result = (Generator(None, scope, operands=value(node.args[0], scope)),)
                # Enumeration preserves the yielded operand in its pair slot.
                result[0].arguments = ("enumerate",)
                return result
            if "map" in names and len(node.args) > 1:
                captured = (value(node.args[0], scope), function_names(node.args[0], scope), tuple(value(argument, scope) for argument in node.args[1:]))
                result = (Generator(node, scope, arguments=captured),)
                expression_values[scope, id(node)] = result
                return result
            if names & {"threading.Thread", "multiprocessing.Process", "threading.Timer"}:
                target = node.args[1] if len(node.args) > 1 else next((keyword.value for keyword in node.keywords if keyword.arg in {"target", "function"}), None)
                position = 2 if "threading.Timer" in names else 3
                args = node.args[position] if len(node.args) > position else next((keyword.value for keyword in node.keywords if keyword.arg == "args"), None)
                kwarg_node = node.args[position + 1] if len(node.args) > position + 1 else next((keyword.value for keyword in node.keywords if keyword.arg == "kwargs"), None)
                sequences = value(args, scope) if args is not None else ([],)
                assigned = [choices(sequence[index] for sequence in sequences if isinstance(sequence, (list, tuple)) and index < len(sequence)) for index in range(max((len(sequence) for sequence in sequences if isinstance(sequence, (list, tuple))), default=0))]
                callbacks = value(target, scope) if target is not None else (unknown,)
                result = (Deferred(Partial(callbacks, assigned, keyword_mapping(kwarg_node, scope))),)
                expression_values[scope, id(node)] = result
                return result
            if names & {"format", "builtins.format"} and 1 <= len(node.args) <= 2 and not node.keywords:
                specs = value(node.args[1], scope) if len(node.args) == 2 else ("",)
                alternatives = [(item, spec) for item in value(node.args[0], scope) for spec in specs]
                result = evaluation_alternatives(alternatives, scope, lambda pair:
                    text_conversion(pair[0], "format", scope, computed if pair[1] is unknown else pair[1])
                    if isinstance(pair[1], str) or pair[1] is unknown else ())
                expression_values[scope, id(node)] = result
                return result
            if len(node.args) == 1 and not node.keywords:
                arguments = value(node.args[0], scope)
                if names & text_conversions.keys():
                    result = choices(text for name in names & text_conversions.keys() for item in arguments for text in text_conversion(item, text_conversions[name], scope))
                    expression_values[scope, id(node)] = result
                    return result
                if "shlex.split" in names:
                    split = []
                    for item in arguments:
                        try:
                            split.append(shlex.split(item) if isinstance(item, str) else unknown)
                        except ValueError:
                            split.append(unknown)
                    return (Sequence(row for row in split if isinstance(row, list)),) if all(isinstance(row, list) for row in split) else choices(split)
                if any(name.endswith(".join") for name in names) and isinstance(node.func, ast.Attribute):
                    rendered = []
                    for separator in value(node.func.value, scope):
                        for argument in arguments:
                            compact = data_join_dag(argument, separator, scope)
                            if compact is not None:
                                rendered.extend(compact)
                                continue
                            rows = iterable_rows((argument,), scope)
                            for sequence in rows:
                                items = iterate_values((sequence,), scope)
                                if isinstance(separator, str) and isinstance(sequence, (list, tuple)):
                                    rendered.append(separator.join(item if isinstance(item, str) else computed for item in sequence))
                                else:
                                    rendered.append(overflow if executable_value(list(items)) or any(executable_value(item) for item in items) else computed)
                    result = choices(rendered)
                    exhaust_generators(arguments)
                    expression_values[scope, id(node)] = result
                    return result
                if names & {"min", "max"}:
                    return iterated(node.args[0], scope) or (unknown,)
                if names & {"list", "tuple", "set", "frozenset", "next", "sorted"}:
                    if "next" in names:
                        return iterated(node.args[0], scope)
                    rows = iterable_rows(arguments, scope)
                    if "sorted" in names:
                        rows = [sorted(row) if all(isinstance(item, str) for item in row) else row for row in rows]
                    result = choices(tuple(row) for row in rows) if "tuple" in names else (Sequence(rows, unique=bool(names & {"set", "frozenset"})),)
                    if names & {"list", "tuple", "sorted"}:
                        exhaust_generators(arguments)
                    expression_values[scope, id(node)] = result
                    return result
            if names & {"functools.partial"} and node.args and not isinstance(node.func, ast.Call):
                result = (Partial(value(node.args[0], scope), expanded_arguments(node.args[1:], scope), call_keywords(node.keywords, scope)),)
                expression_values[scope, id(node)] = result
                return result
            if "operator.methodcaller" in names and node.args and not isinstance(node.func, ast.Call):
                methods = value(node.args[0], scope)
                result = (Partial(tuple(ast.alias(name="methodcaller." + (method if isinstance(method, str) else computed)) for method in methods),
                                  expanded_arguments(node.args[1:], scope), call_keywords(node.keywords, scope)),)
                expression_values[scope, id(node)] = result
                return result
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"parse_args", "add_subparsers", "add_parser"}:
                result = choices(owner for owner in value(node.func.value, scope) if isinstance(owner, Registry))
                if result:
                    expression_values[scope, id(node)] = result
                    return result
            if names & {"typer.Typer", "argparse.ArgumentParser"}:
                result = (Registry(),)
                expression_values[scope, id(node)] = result
                return result
            functions = value(node.func, scope)
            constructed = [construct(kind, scope, expanded_arguments(node.args, scope), call_keywords(node.keywords, scope))
                           for kind in functions if isinstance(kind, ast.ClassDef)]
            if constructed:
                result = choices(constructed)
                expression_values[scope, id(node)] = result
                return result
            constructors = {name for name in names if name in {"concurrent.futures.ThreadPoolExecutor", "concurrent.futures.ProcessPoolExecutor", "multiprocessing.Pool", "multiprocessing.pool.ThreadPool"}}
            if constructors:
                return choices(ast.alias(name=name) for name in constructors)
            results = []
            for function in functions:
                results.extend(invoke(function, scope, node.args, node.keywords, unknown_effect=False))
            if results:
                result = choices(results)
                expression_values[scope, id(node)] = result
                return result
        # Unknown transforms retain uncertainty in their eventual operand
        # position; ownership-shaped input is never executed on its own.
        def possible_code(item, seen=()):
            if isinstance(item, (Sequence, Keywords, list, tuple)):
                if id(item) in seen:
                    return False
                descendants = (*seen, id(item))
                children = item.items.values() if isinstance(item, Keywords) else sequence_members(item) if isinstance(item, Sequence) else item
                return any(possible_code(child, descendants) for child in children)
            return isinstance(item, str) and executable_operand(item)
        if node is not None:
            operands = [item for child in ast.iter_child_nodes(node) for item in value(child, scope)]
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                operands.extend(value(node.func.value, scope))
            generators = [item for item in operands if isinstance(item, Generator)]
            if generators:
                return (Generator(None, scope, operands=generators), *((overflow,) if any(possible_code(item) for item in operands) else ()))
            if any(possible_code(item) for item in operands):
                return (overflow,)
        return (unknown,)

    def add(left, right):
        if (left and right and (len(left) > 1 or len(right) > 1)
                and all(isinstance(item, Sequence) and not item.unique for item in left)
                and all(isinstance(item, Sequence) and not item.unique or type(item) is list for item in right)):
            # Addition creates one fresh list. Share its alternative operand
            # snapshots instead of duplicating every prior branch's DAG.
            return (RowConcat(choices(row for item in left for row in item.state["rows"]),
                              choices(row for item in right for row in (item.state["rows"] if isinstance(item, Sequence) else (item,)))),)
        # Newline-closed modules can execute as sequential alternative blocks.
        # This retains every source while avoiding Cartesian fixture strings.
        source_left = [item for item in left if isinstance(item, str) and parsed_source(item, "exec") is not None]
        source_right = [item for item in right if isinstance(item, str) and parsed_source(item, "exec") is not None]
        factored = None
        if source_left and source_right and len(source_left) * len(source_right) > 8:
            if (all(not item or item.endswith("\n") for item in source_left)
                    or all(not item or item.startswith("\n") for item in source_right)):
                factored = source_alternative(source_left) + source_alternative(source_right)
                factored_sources.add(factored)
            elif len(source_left) * len(source_right) > 8 and any(
                    isinstance(node, (ast.Call, ast.Import, ast.ImportFrom))
                    for source in (*source_left, *source_right)
                    for node in ast.walk(parsed_source(source, "exec"))):
                # Nonseparable expressions cannot use module branch merging.
                # Keep an owning witness rather than enumerate or silently
                # discard the source products beyond the usual value bound.
                factored = overflow
        result = [factored] if factored is not None else []
        source_left_set, source_right_set = set(source_left), set(source_right)
        remaining_right = tuple(item for item in right if not isinstance(item, str) or item not in source_right_set)
        # Apply the operation before bounding its results. Bounding pairs first
        # loses the executable identity of a later helper argv prefix.
        for assigned_first in left:
            seconds = remaining_right if factored is not None and isinstance(assigned_first, str) and assigned_first in source_left_set else right
            for assigned_second in seconds:
                if isinstance(assigned_first, Sequence) and not assigned_first.unique:
                    if isinstance(assigned_second, Sequence) and not assigned_second.unique:
                        result.append(RowConcat(assigned_first.state["rows"], assigned_second.state["rows"]))
                        continue
                    if type(assigned_second) is list:
                        result.append(RowConcat(assigned_first.state["rows"], (assigned_second,)))
                        continue
                for first in sequence_rows(assigned_first):
                    for second in sequence_rows(assigned_second):
                        if isinstance(first, str) and second is unknown:
                            second = computed
                        if isinstance(second, str) and first is unknown:
                            first = computed
                        result.append(first + second if isinstance(first, (str, list, tuple)) and type(first) is type(second) else unknown)
        return choices(result)

    def overflow_execution(command):
        if overflow not in command:
            return False
        for argv in shell_commands("exec_command", {"cmd": command}):
            child = unwrapped_command(argv)
            if child and overflow in child[0]:
                return True
            if child and Path(child[0]).name in {"bash", "sh", "zsh", "dash", "ksh"}:
                body = shell_c_body(child)
                if body is not None and overflow_execution(body):
                    return True
            if any(overflow_execution(body) for body in executor_commands(child)):
                return True
        return False

    def binding_key(item, seen=(), memo=None):
        # Compact interned DAG nodes preserve sharing. A recursively nested
        # tuple would still grow exponentially when the cache hashes its key.
        if memo is None:
            memo = {}
        identity = id(item)
        if identity in seen:
            key = ("recursive", identity)
        elif identity in memo:
            return memo[identity][1]
        else:
            descendants = (*seen, identity)
            def child(assigned):
                return binding_key(assigned, descendants, memo)
            if isinstance(item, Function):
                # Equal current values do not identify live closure cells from
                # distinct calls. Keep the owning frame itself, including an
                # unassigned local, alongside its current value fingerprint.
                key = ("function", id(item.node), tuple(sorted(
                    (name, owner if owner is not None and owner.local_names is not None else None,
                     child(item.scope.get(name, (unknown,))))
                    for name in item.free for owner in (item.scope.owner(name),))))
            elif isinstance(item, Descriptor):
                key = ("descriptor", item.kind, child(item.function), child(item.setter), child(item.deleter))
            elif isinstance(item, Bound):
                key = ("bound", child(item.function), child(item.receiver))
            elif isinstance(item, Instance):
                key = ("instance", identity, child(item.kind), child(item.json_payload), tuple(sorted((child(name), child(assigned)) for name, assigned in item.attributes.items())))
            elif isinstance(item, Registry):
                key = ("registry", identity, child(item.handlers))
            elif isinstance(item, Partial):
                key = ("partial", child(item.functions), child(item.arguments), tuple(sorted((str(name), child(assigned)) for name, assigned in item.keywords.items())))
            elif isinstance(item, CompiledCode):
                key = ("code", item.source)
            elif isinstance(item, PatchContext):
                key = ("patch", item.target, child(item.replacement), item.inject, child(item.children), child(item.attribute_owner), item.create)
            elif isinstance(item, ExceptionValue):
                key = ("exception", item.name)
            elif isinstance(item, Sequence):
                key = ("mutable-sequence", child(item.state["rows"]))
            elif isinstance(item, RowConcat):
                def intern(key):
                    return fingerprints.setdefault(key, len(fingerprints))
                def alternatives(tokens):
                    return intern(("sequence", tuple(tokens)))
                token = row_dag_values((item,), child,
                    lambda left, right: intern(("row-concat", left, right)), alternatives)[0]
                memo[identity] = item, token
                return token
            elif isinstance(item, Keywords):
                key = ("mapping", tuple(sorted((str(name), child(assigned)) for name, assigned in item.items.items())))
            elif isinstance(item, ast.alias):
                key = ("alias", item.name)
            elif isinstance(item, ast.ClassDef):
                key = ("class", identity, child(class_bases.get(identity, ())), tuple(sorted((name, child(assigned)) for name, assigned in class_bindings.get(identity, {}).items())))
            elif isinstance(item, ast.AST):
                key = ("node", identity)
            elif isinstance(item, (list, tuple)):
                key = ("sequence", tuple(child(assigned) for assigned in item))
            else:
                key = (type(item).__name__, repr(item))
        token = fingerprints.setdefault(key, len(fingerprints))
        memo[identity] = item, token
        return token

    def keyword_mapping(node, scope):
        result = {}
        if node is not None:
            for mapping in value(node, scope):
                if isinstance(mapping, Keywords):
                    for name, assigned in mapping.items.items():
                        key = name if isinstance(name, str) else unknown
                        result[key] = choices((*result.get(key, ()), *assigned))
        return result

    def call_keywords(keywords, scope):
        result = {}
        for keyword in keywords:
            if keyword.arg is None:
                update_mapping(result, keyword_mapping(keyword.value, scope))
            else:
                result[keyword.arg] = value(keyword.value, scope)
        return result

    def expanded_arguments(arguments, scope):
        result = []
        for argument in arguments:
            if isinstance(argument, ast.Starred):
                sequences = value(argument.value, scope)
                if any(isinstance(sequence, Generator) for sequence in sequences):
                    items = iterate_values(sequences, scope)
                    if all(isinstance(sequence, Generator) and sequence.positions is not None for sequence in sequences):
                        length = max((len(sequence.positions) for sequence in sequences), default=0)
                        result.extend(choices(item for sequence in sequences if index < len(sequence.positions) for item in sequence.positions[index]) for index in range(length))
                    else:
                        result.extend((item,) for item in items)
                    continue
                sequences = tuple(row for sequence in sequences for row in sequence_rows(sequence))
                length = max((len(sequence) for sequence in sequences if isinstance(sequence, (list, tuple))), default=0)
                for index in range(length):
                    result.append(choices(sequence[index] if isinstance(sequence, (list, tuple)) and index < len(sequence) else unknown for sequence in sequences))
            else:
                result.append(value(argument, scope))
        return result

    def effectful(item, seen=(), memo=None):
        if memo is None:
            memo = {}
        identity = (id(item.function), id(item.receiver)) if isinstance(item, Bound) else id(item)
        if identity in seen:
            return False
        if identity in memo:
            return memo[identity]
        descendants = (*seen, identity)
        if isinstance(item, Function):
            result = any(isinstance(node, (ast.Global, ast.Nonlocal))
                         or isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, ast.Store)
                         or isinstance(node, ast.Call) and function_names(node.func, item.scope) & {"atexit.register", "atexit.unregister"}
                         for node in ast.walk(item.node))
            result |= any(effectful(assigned, descendants, memo) for name in item.free for assigned in item.scope.get(name, ()))
        elif isinstance(item, Descriptor):
            result = any(effectful(callback, descendants, memo) for callback in (item.function, *item.setter, *item.deleter))
        elif isinstance(item, Bound):
            if isinstance(item.function, ast.alias) and item.function.name.startswith(("methodcaller.", "patch.", "iterator.")):
                return True
            result = effectful(item.function, descendants, memo)
            if not isinstance(item.function, Function):
                return result
            parameters = [*item.function.node.args.posonlyargs, *item.function.node.args.args]
            receiver_name = parameters[0].arg if parameters else None
            result |= any(effectful(method, descendants, memo)
                          for node in ast.walk(item.function.node)
                          if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                          and isinstance(node.func.value, ast.Name) and node.func.value.id == receiver_name
                          for method in member(item.receiver, node.func.attr))
        elif isinstance(item, (Instance, Registry, Deferred, PatchContext)):
            result = True
        elif isinstance(item, ast.ClassDef):
            result = any(effectful(assigned, descendants, memo) for assigned in class_bindings.get(identity, {}).get("__init__", ()))
        elif isinstance(item, Partial):
            result = any(effectful(function, descendants, memo) for function in item.functions)
        elif isinstance(item, (list, tuple)):
            result = any(effectful(child, descendants, memo) for child in item)
        else:
            result = False
        memo[identity] = result
        return result

    def captures_generator(item, seen=None):
        """A live iterator can read late bindings that an identity key omits."""
        if isinstance(item, Generator):
            return True
        seen = set() if seen is None else seen
        if id(item) in seen:
            return False
        seen.add(id(item))
        if isinstance(item, Function):
            children = [*item.defaults.values(), *(item.scope.get(name, ()) for name in item.free)]
        elif isinstance(item, Descriptor):
            children = [item.function, *item.setter, *item.deleter]
        elif isinstance(item, Bound):
            children = [item.function, item.receiver]
        elif isinstance(item, PatchContext):
            children = [item.replacement, item.children, item.attribute_owner]
        elif isinstance(item, Instance):
            children = [item.kind, *item.attributes.values()]
        elif isinstance(item, ast.ClassDef):
            children = [*class_bases.get(id(item), ()), *class_bindings.get(id(item), {}).values()]
        elif isinstance(item, Partial):
            children = [item.functions, item.arguments, *item.keywords.values()]
        elif isinstance(item, Sequence):
            children = sequence_members(item)
        elif isinstance(item, Keywords):
            children = item.items.values()
        elif isinstance(item, (list, tuple)):
            children = item
        else:
            return False
        return any(captures_generator(child, seen) for child in children)

    def reusable_result(item, seen=()):
        if id(item) in seen:
            return True
        descendants = (*seen, id(item))
        if isinstance(item, (Instance, Registry, Sequence, Keywords, Generator, PatchContext, ast.ClassDef)):
            return False
        if isinstance(item, Function):
            return not effectful(item) and all(reusable_result(assigned, descendants) for assigned in [*item.defaults.values(), *(item.scope.get(name, ()) for name in item.free)])
        if isinstance(item, Descriptor):
            return all(reusable_result(callback, descendants) for callback in (item.function, *item.setter, *item.deleter))
        if isinstance(item, Bound):
            return reusable_result(item.function, descendants) and reusable_result(item.receiver, descendants)
        if isinstance(item, Partial):
            return all(reusable_result(assigned, descendants) for assigned in [item.functions, item.arguments, *item.keywords.values()])
        if isinstance(item, Deferred):
            return reusable_result(item.callback, descendants)
        return all(reusable_result(child, descendants) for child in item) if isinstance(item, (list, tuple)) else True

    def construct(kind, scope, arguments, keywords):
        nonlocal attribute_mutations
        instance = Instance(kind)
        initializers = member(instance, "__init__")
        if initializers:
            for initializer in initializers:
                invoke(initializer, scope, arguments, keywords, evaluated=True)
        elif any(function_names(decorator.func if isinstance(decorator, ast.Call) else decorator, scope) & {"dataclasses.dataclass"} for decorator in kind.decorator_list):
            fields = [statement.target.id for statement in kind.body if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)]
            supplied = dict(zip(fields, arguments))
            supplied.update(keywords)
            for name in fields:
                attribute_mutations += 1
                observe(instance.attributes)
                instance.attributes[name] = supplied.get(name, class_bindings.get(id(kind), {}).get(name, (unknown,)))
            for hook in member(instance, "__post_init__"):
                invoke(hook, scope)
        native = json_native_kind(kind)
        if native == "int":
            # PyNumber_Index reads an int subclass's stored value without
            # calling an overridden __index__.
            if class_member(kind, "__new__") or keywords or len(arguments) > 1:
                instance.json_payload = (unknown,)
            elif arguments:
                instance.json_payload = tuple(item if type(item) in (int, bool) else unknown for item in arguments[0])
            else:
                instance.json_payload = (0,)
        if native in {"list", "tuple", "dict"}:
            if initializers:
                instance.json_payload = (unknown,)
            elif native == "dict":
                instance.json_payload = data_call_protocol("dict", arguments, keywords, scope)
            else:
                instance.json_payload = (Sequence((list(iterate_values(arguments[0], scope)) if arguments else [],)),)
        return instance

    def invoke(wrapper, caller, arguments=(), keywords=(), *, evaluated=False, consumed=False, yielded=None, unknown_effect=True, created_active=None):
        nonlocal found, recursion_skips, expression_values
        argument_values = list(arguments) if evaluated else expanded_arguments(arguments, caller)
        keyword_values = dict(keywords) if evaluated else call_keywords(keywords, caller)
        if isinstance(wrapper, Bound) and isinstance(wrapper.function, ast.alias) and wrapper.function.name == "json.container":
            item = wrapper.receiver
            boolean = member(item, "__bool__", caller, special=True)
            callbacks = boolean or member(item, "__len__", caller, special=True)
            outputs = choices(result for callback in callbacks for result in invoke(callback, caller))
            if not boolean:
                outputs = choices(indexed for output in outputs
                                  for indexed in (output.json_payload if isinstance(output, Instance) and json_native_kind(output.kind) == "int"
                                                  else data_call_protocol("index", [(output,)], {}, caller)
                                                  if isinstance(output, Instance) else (output,)))
            if not any(result is unknown or (type(result) is bool if boolean else type(result) in (int, bool)) and 0 < result <= sys.maxsize
                       for result in outputs):
                return ()
            native = json_native_kind(item.kind)
            protocols = member(item, "items" if native == "dict" else "__iter__", caller, special=native != "dict")
            # The outer iterable consumer must retain the built-in payload's
            # shape so dictionary values reach JSON's normal leaf traversal.
            return choices(result for callback in protocols for result in invoke(callback, caller)) if protocols else (item.json_payload,)
        if isinstance(wrapper, Bound) and isinstance(wrapper.function, ast.alias) and wrapper.function.name == "json.iterate":
            returned = invoke(wrapper.receiver, caller, argument_values, keyword_values, evaluated=True)
            return iterate_values(returned, caller)
        if isinstance(wrapper, ast.alias) and wrapper.name in async_consumers:
            return async_result(wrapper.name, argument_values, keyword_values, caller)
        if isinstance(wrapper, ast.alias) and wrapper.name in mock_types:
            return (make_mock(wrapper.name, keyword_values),)
        if isinstance(wrapper, ast.alias) and wrapper.name in protocol_data_calls:
            return data_call_protocol(wrapper.name, argument_values, keyword_values, caller)
        if isinstance(wrapper, ast.alias) and wrapper.name in text_conversions and len(argument_values) == 1 and not keyword_values:
            return choices(text for item in argument_values[0] for text in text_conversion(item, text_conversions[wrapper.name], caller))
        if isinstance(wrapper, ast.alias) and wrapper.name in {"print", "builtins.print"}:
            flush_flags = []
            for flag in keyword_values.get("flush", (False,)):
                if isinstance(flag, Instance):
                    boolean = member(flag, "__bool__", caller, special=True)
                    callbacks = boolean or member(flag, "__len__", caller, special=True)
                    returned = choices(item for callback in callbacks for item in invoke(callback, caller))
                    if boolean:
                        flush_flags.extend(item for item in returned if type(item) is bool)
                    else:
                        flush_flags.extend(bool(item) for item in returned if type(item) in (int, bool) and item >= 0)
                    if not callbacks or any(item is unknown for item in returned):
                        flush_flags.append(True)
                else:
                    try:
                        flush_flags.append(bool(data_literal(flag)))
                    except InexactDataLoop:
                        flush_flags.append(True)
            if not flush_flags:
                return (None,)
            separators = keyword_values.get("sep", (" ",))
            endings = keyword_values.get("end", ("\n",))
            if (not any(item is None or isinstance(item, str) or item is unknown for item in separators)
                    or not any(item is None or isinstance(item, str) or item is unknown for item in endings)):
                return (None,)
            destinations = choices(destination for item in keyword_values.get("file", (None,))
                for destination in (patched_attributes.get("sys.stdout", (ast.alias(name="sys.stdout"),)) if item is None else (item,)))
            def rendered(items):
                return choices(text for item in items for text in text_conversion(item, "str", caller))
            for destination in destinations:
                if destination is None:
                    continue
                def write(items, convert=False):
                    callbacks = patched_attributes.get(destination.name + ".write", (ast.alias(name=destination.name + ".write"),)) if isinstance(destination, ast.alias) else (unknown,) if destination is unknown else member(destination, "write", caller)
                    if not callbacks:
                        return False
                    chunks = rendered(items) if convert else choices(" " if item is None else item for item in items)
                    if not chunks:
                        return False
                    for callback in callbacks:
                        invoke(callback, caller, [chunks], evaluated=True)
                    return True
                for index, items in enumerate(argument_values):
                    if index and not write(separators):
                        break
                    if not write(items, convert=True):
                        break
                else:
                    if write(tuple("\n" if item is None else item for item in endings)) and any(flush_flags):
                        callbacks = patched_attributes.get(destination.name + ".flush", (ast.alias(name=destination.name + ".flush"),)) if isinstance(destination, ast.alias) else member(destination, "flush", caller)
                        for callback in callbacks:
                            invoke(callback, caller)
            return (None,)
        if isinstance(wrapper, Bound) and isinstance(wrapper.function, PatchContext):
            def patched_call(callback, patches):
                if isinstance(callback, Bound) and isinstance(callback.function, PatchContext):
                    return choices(item for function in callback.receiver for item in patched_call(function, [callback.function, *patches]))
                supplied, supplied_keywords, exits = list(argument_values), dict(keyword_values), []
                try:
                    for patch in patches:
                        entered, callbacks = enter_context((patch,), caller)
                        exits.extend(callbacks)
                        if patch.inject:
                            supplied.append(entered)
                        if patch.children:
                            for mapping in entered:
                                if isinstance(mapping, Keywords):
                                    supplied_keywords.update(mapping.items)
                    return invoke(callback, caller, supplied, supplied_keywords, evaluated=True,
                                  consumed=consumed, yielded=yielded, unknown_effect=unknown_effect, created_active=created_active)
                finally:
                    for context in reversed(exits):
                        close_context(context, caller)
            return patched_call(wrapper, [])
        if isinstance(wrapper, Bound) and isinstance(wrapper.function, ast.alias):
            if wrapper.function.name.startswith("iterator.") and is_value_iterator(wrapper.receiver):
                return (wrapper.receiver,) if wrapper.function.name == "iterator.__iter__" else consume_iterator(wrapper.receiver, caller)
            if wrapper.function.name == "patch.stopall":
                for assigned in reversed(tuple(started_patches.values())):
                    for patch in assigned:
                        if isinstance(patch, PatchContext) and patch.previous:
                            close_context(patch, caller)
                observe(started_patches)
                started_patches.clear()
                return (None,)
            if wrapper.function.name.startswith("patch.") and isinstance(wrapper.receiver, PatchContext):
                if wrapper.function.name == "patch.start":
                    entered = enter_context((wrapper.receiver,), caller)[0]
                    observe(started_patches)
                    started_patches[id(wrapper.receiver)] = (wrapper.receiver,)
                    return entered
                if id(wrapper.receiver) in started_patches and wrapper.receiver.previous:
                    close_context(wrapper.receiver, caller)
                    observe(started_patches)
                    started_patches.pop(id(wrapper.receiver), None)
                return (None,)
            if wrapper.function.name.startswith("methodcaller.") and isinstance(wrapper.receiver, (Sequence, Keywords)):
                return mutate_values([wrapper.receiver], wrapper.function.name.removeprefix("methodcaller."), argument_values, keyword_values, caller)
        if isinstance(wrapper, Bound) and not isinstance(wrapper.function, Function):
            return invoke(wrapper.function, caller, [(wrapper.receiver,), *argument_values], keyword_values, evaluated=True,
                          consumed=consumed, yielded=yielded, unknown_effect=unknown_effect, created_active=created_active)
        if isinstance(wrapper, Descriptor) and wrapper.kind == "staticmethod":
            return invoke(wrapper.function, caller, argument_values, keyword_values, evaluated=True,
                          consumed=consumed, yielded=yielded, unknown_effect=unknown_effect, created_active=created_active)
        if isinstance(wrapper, Descriptor) and wrapper.kind in {"getter", "setter", "deleter"}:
            original = wrapper.function
            callbacks = argument_values[0] if argument_values else keyword_values.get("f" + wrapper.kind[:3], (None,))
            if wrapper.kind == "getter":
                return tuple(Descriptor(callback, "property", original.setter, original.deleter) for callback in callbacks)
            return (Descriptor(original.function, "property", callbacks if wrapper.kind == "setter" else original.setter,
                               callbacks if wrapper.kind == "deleter" else original.deleter),)
        if isinstance(wrapper, Partial):
            combined_keywords = dict(wrapper.keywords)
            update_mapping(combined_keywords, keyword_values)
            return choices(item for function in wrapper.functions for item in invoke(function, caller, [*wrapper.arguments, *argument_values], combined_keywords, evaluated=True, consumed=consumed, yielded=yielded, created_active=created_active))
        if isinstance(wrapper, ast.ClassDef):
            return (construct(wrapper, caller, argument_values, keyword_values),)
        if isinstance(wrapper, Instance):
            if isinstance(wrapper.kind, ast.alias) and wrapper.kind.name in mock_types:
                if wrapper.kind.name.rsplit(".", 1)[-1].startswith("NonCallable"):
                    return (None,)
                if wrapper.kind.name == "unittest.mock.AsyncMock" and not consumed:
                    return (Deferred(Partial((wrapper,), argument_values, keyword_values)),)
                def unresolved_callback(callback):
                    if (isinstance(callback, Bound) and isinstance(callback.function, ast.alias)
                            and callback.function.name.startswith("methodcaller.") and isinstance(callback.receiver, (Sequence, Keywords))):
                        return False
                    if (isinstance(callback, Bound) and isinstance(callback.function, ast.alias)
                            and callback.function.name.startswith("iterator.") and is_value_iterator(callback.receiver)):
                        return False
                    return (callback is unknown or isinstance(callback, ast.alias) and callback.name not in launches | pure_data_calls | exception_types | {"unittest.mock.DEFAULT"}
                            or isinstance(callback, Bound) and unresolved_callback(callback.function))
                def call_mock_callback(callback):
                    nonlocal found
                    # Mock behavior may be assigned after the patch is entered.
                    # Such an opaque callback can still invoke the replaced sink.
                    if unresolved_callback(callback):
                        found |= any(executable_value(command) for assigned in [*argument_values, *keyword_values.values()] for command in assigned)
                    return invoke(callback, caller, argument_values, keyword_values, evaluated=True, unknown_effect=unknown_effect)
                side_effects = wrapper.attributes.get("side_effect", ())
                if any(isinstance(item, (Sequence, Keywords, Generator, list, tuple)) for item in side_effects):
                    side_effects = choices(make_iterator((item,)) if isinstance(item, (Sequence, Keywords, Generator, list, tuple)) else item for item in side_effects)
                    observe(wrapper.attributes)
                    wrapper.attributes["side_effect"] = side_effects
                results, fallback = [], not side_effects or any(item is None for item in side_effects)
                for callback in side_effects:
                    if callback is None or isinstance(callback, ExceptionValue) or isinstance(callback, ast.alias) and callback.name in exception_types:
                        continue
                    returned = consume_iterator(callback, caller) if is_value_iterator(callback) else call_mock_callback(callback)
                    results.extend(item for item in returned if not (isinstance(item, ast.alias) and item.name == "unittest.mock.DEFAULT"))
                    fallback |= any(isinstance(item, ast.alias) and item.name == "unittest.mock.DEFAULT" for item in returned)
                if fallback:
                    returns = wrapper.attributes.get("return_value", ())
                    results.extend(item for item in returns if not (isinstance(item, ast.alias) and item.name == "unittest.mock.DEFAULT"))
                    if not returns or any(isinstance(item, ast.alias) and item.name == "unittest.mock.DEFAULT" for item in returns):
                        results.extend(item for callback in wrapper.attributes.get("_mock_wraps", ()) for item in call_mock_callback(callback))
                return choices(results) or (None,)
            return choices(item for callback in member(wrapper, "__call__")
                           for item in invoke(callback, caller, argument_values, keyword_values, evaluated=True,
                                              consumed=consumed, yielded=yielded, unknown_effect=unknown_effect, created_active=created_active))
        if isinstance(wrapper, ast.alias) and wrapper.name in launches:
            parameter = "args" if wrapper.name.startswith("subprocess.") else "cmd" if wrapper.name == "os.popen" else "command"
            commands = argument_values[0] if argument_values else keyword_values.get(parameter, ())
            found |= any(executable_value(command) for command in commands)
            return (unknown,)
        if isinstance(wrapper, ast.alias) and (wrapper.name in external_mutations or wrapper.name.startswith("methodcaller.")):
            method = wrapper.name.removeprefix("methodcaller.") if wrapper.name.startswith("methodcaller.") else "append"
            receiver = argument_values[-1:] if wrapper.name.startswith("methodcaller.") else argument_values[:1]
            operands = argument_values[:-1] if wrapper.name.startswith("methodcaller.") else argument_values[1:]
            owners = [owner for assigned in receiver for owner in assigned if isinstance(owner, (Sequence, Keywords))]
            if method in mutation_methods:
                return mutate_values(owners, method, operands, keyword_values, caller)
            escape_mutables(receiver)
            return (unknown,)
        if isinstance(wrapper, Registry):
            for handler in wrapper.handlers:
                invoke(handler, caller)
            return (wrapper,)
        receiver = None
        if isinstance(wrapper, Bound):
            wrapper, receiver = wrapper.function, wrapper.receiver
        if not isinstance(wrapper, Function):
            if not (isinstance(wrapper, ast.alias) and wrapper.name in pure_data_calls):
                deferred = tuple(deferred_operands([*argument_values, *keyword_values.values()]))
                if unknown_effect:
                    escape_mutables([*argument_values, *keyword_values.values()])
                if deferred:
                    return choices((*consume_deferred(deferred, caller), unknown))
            return ()
        node = wrapper.node
        if not consumed and wrapper.generator:
            return (Generator(Bound(wrapper, receiver) if receiver is not None else wrapper, caller, argument_values, keyword_values),)
        # Pre-created iterator adapters may nest the same function finitely.
        # Iterators created by that active function still obey its recursion bound.
        if active.count(node) >= 2 and (created_active is None or node in created_active):
            recursion_skips += 1
            return (unknown,)
        local = Scope(wrapper.scope, wrapper.locals, wrapper.globals, wrapper.nonlocals)
        parameters = [*node.args.posonlyargs, *node.args.args]
        if receiver is not None:
            if parameters:
                local[parameters.pop(0).arg] = (receiver,)
            else:
                argument_values.insert(0, (receiver,))
        supplied = {parameter.arg: assigned for parameter, assigned in zip(parameters, argument_values)}
        supplied.update(keyword_values)
        positional = {parameter.arg for parameter in parameters[:len(argument_values)]}
        for parameter in (*parameters, *node.args.kwonlyargs):
            assigned = supplied.get(parameter.arg, wrapper.defaults.get(parameter.arg, (unknown,)))
            if parameter.arg not in positional and parameter not in node.args.posonlyargs:
                assigned = choices((*assigned, *keyword_values.get(unknown, ())))
            local[parameter.arg] = assigned
        if node.args.vararg is not None:
            local[node.args.vararg.arg] = choices(combinations(argument_values[len(parameters):]))
        if node.args.kwarg is not None:
            names = {parameter.arg for parameter in (*parameters, *node.args.kwonlyargs)}
            local[node.args.kwarg.arg] = (Keywords({name: assigned for name, assigned in keyword_values.items() if name not in names}),)
        cache_key = (binding_key(wrapper), tuple(sorted((name, binding_key(assigned)) for name, assigned in local.state.items())),
                     tuple(sorted((name, binding_key(assigned)) for name, assigned in patched_attributes.items())))
        # A pure method depends on its receiver attributes just like lexical
        # inputs. Mutating bodies still execute on every actual invocation.
        invoked = Bound(wrapper, receiver) if receiver is not None else wrapper
        cacheable = not captures_generator([invoked, *local.state.values()]) and not effectful(invoked) and not any(
            effectful(assigned) for assigned in local.state.values()
            if not (receiver is not None and len(assigned) == 1 and assigned[0] is receiver))
        if cacheable and cache_key in invocation_cache:
            owning, result, collected = invocation_cache[cache_key]
            found |= owning
            if yielded is not None:
                yielded.update(collected)
            return result
        previous, generation, mutation_start = found, recursion_skips, attribute_mutations
        found = False
        active.append(node)
        expressions, expression_values = expression_values, {}
        returned = []
        return_values.append(returned)
        yield_values.append({})
        try:
            if isinstance(node, ast.Lambda):
                scan(node.body, local)
                returned.extend(value(node.body, local))
            else:
                walk(node.body, local)
        finally:
            return_values.pop()
            collected = yield_values.pop()
            if yielded is not None:
                yielded.update(collected)
            expression_values = expressions
            active.pop()
        owning, result = found, choices(returned) or (None,)
        found |= previous
        if (cacheable and recursion_skips == generation and attribute_mutations == mutation_start
                and reusable_result(result) and all(reusable_result(assigned) for assigned in collected.values())):
            invocation_cache[cache_key] = owning, result, collected
        return result

    mutation_methods = {"append", "extend", "insert", "add", "update", "setdefault", "clear", "pop", "remove", "discard", "reverse", "sort", "copy"}
    external_mutations = {"heapq.heappush", "bisect.insort", "bisect.insort_left", "bisect.insort_right"}
    pure_data_calls = {
        "print", "str", "repr", "ascii", "builtins.str", "builtins.repr", "builtins.ascii", "format", "builtins.format", "len", "bool", "int", "float", "bytes", "complex", "hash", "type", "id", "isinstance", "issubclass",
        "list", "tuple", "set", "frozenset", "dict", "iter", "next", "all", "any", "sum", "min", "max", "sorted", "enumerate", "zip", "map", "range",
        "getattr", "hasattr", "vars", "dir", "inspect.signature", "pprint.pprint", "pprint.pformat", "json.dumps", "json.loads",
        "shlex.quote", "shlex.split", "shlex.join", "functools.partial", "operator.methodcaller", "atexit.register", "atexit.unregister",
        "builtins.iter", "builtins.next", "itertools.cycle",
        "threading.Thread", "threading.Timer", "multiprocessing.Process", "collections.deque", "contextlib.ExitStack", "contextlib.AsyncExitStack",
    } | protocol_data_calls

    def escape_mutables(arguments, seen=None):
        """An unresolved callee cannot prove an escaped container unchanged."""
        nonlocal attribute_mutations
        seen = set() if seen is None else seen
        for assigned in arguments:
            for owner in assigned:
                if not isinstance(owner, (Sequence, Keywords, list, tuple)) or id(owner) in seen:
                    continue
                seen.add(id(owner))
                if isinstance(owner, (list, tuple)):
                    escape_mutables((owner,), seen)
                    continue
                children = list(owner.items.values()) if isinstance(owner, Keywords) else [tuple(row) for row in sequence_rows(owner)]
                if isinstance(owner, Keywords):
                    previous = owner.items.get(unknown, ())
                    updated = choices((*previous, unknown))
                    if updated != previous:
                        observe(owner.state)
                        attribute_mutations += 1
                        owner.items[unknown] = updated
                else:
                    rows = sequence_rows(owner)
                    updated = choices((*rows, *(list(row) + [unknown] for row in rows if not any(item is unknown for item in row))))
                    if updated != owner.state["rows"]:
                        observe(owner.state)
                        attribute_mutations += 1
                        owner.state["rows"] = updated
                escape_mutables(children, seen)

    def is_exit_stack(owner):
        return isinstance(owner, Instance) and isinstance(owner.kind, ast.alias) and owner.kind.name in {"contextlib.ExitStack", "contextlib.AsyncExitStack"}

    def close_context(owner, scope):
        nonlocal attribute_mutations
        if isinstance(owner, PatchContext):
            attribute_mutations += 1
            previous = owner.previous.pop()
            if owner.children:
                for child in reversed(owner.children):
                    if child.previous:
                        close_context(child, scope)
                return
            mapping, attribute = patch_binding(owner)
            if isinstance(owner.attribute_owner, Instance) and isinstance(previous, dict):
                if previous["local"] is not None:
                    assign_attribute(owner.attribute_owner, attribute, previous["local"], scope)
                else:
                    delete_attribute(owner.attribute_owner, attribute, scope)
                    if not owner.create:
                        present = member(owner.attribute_owner, attribute, scope)
                        if not present or any(item is unknown for item in present):
                            assign_attribute(owner.attribute_owner, attribute, previous["original"], scope)
            elif mapping is not None:
                observe(mapping)
                if previous is None:
                    mapping.pop(attribute, None)
                else:
                    mapping[attribute] = previous
            expression_values.clear()
            return
        callbacks = owner.attributes.get("_context_exits", ())
        observe(owner.attributes)
        owner.attributes["_context_exits"] = ()
        for callback in reversed(callbacks):
            if isinstance(callback, PatchContext) or is_exit_stack(callback):
                close_context(callback, scope)
            else:
                invoke(callback, scope, evaluated=True)

    def enter_context(assigned, scope, *, async_=False):
        nonlocal attribute_mutations
        results, exits = [], []
        for owner in assigned:
            if isinstance(owner, PatchContext):
                attribute_mutations += 1
                if owner.children:
                    owner.previous.append(None)
                    injected = {}
                    for child in owner.children:
                        entered, _ = enter_context((child,), scope)
                        if child.inject:
                            injected[child.target.rsplit(".", 1)[-1]] = entered
                    results.append(Keywords(injected))
                    exits.append(owner)
                    continue
                mapping, attribute = patch_binding(owner)
                previous = mapping.get(attribute) if mapping is not None else None
                if isinstance(owner.attribute_owner, Instance):
                    original = previous if previous is not None else member(owner.attribute_owner, attribute, scope)
                    owner.previous.append({"local": previous, "original": original})
                else:
                    owner.previous.append(previous)
                    original = (mapping.get(attribute, (ast.alias(name=attribute),)) if owner.attribute_owner is None else
                                member(owner.attribute_owner, attribute, scope))
                replacement = owner.replacement
                if any(item is unknown or isinstance(item, ast.alias) and item.name not in pure_data_calls for item in replacement):
                    replacement = choices((*replacement, *original))
                if isinstance(owner.attribute_owner, Instance):
                    assign_attribute(owner.attribute_owner, attribute, replacement, scope)
                elif mapping is not None:
                    observe(mapping)
                    mapping[attribute] = replacement
                expression_values.clear()
                results.extend(replacement)
                exits.append(owner)
            elif isinstance(owner, Generator):
                results.extend(iterate_values((owner,), scope))
            elif is_exit_stack(owner):
                results.append(owner)
                exits.append(owner)
            elif isinstance(owner, Instance):
                for function in member(owner, "__aenter__" if async_ else "__enter__"):
                    results.extend(invoke(function, scope))
                exits.append(Partial(member(owner, "__aexit__" if async_ else "__exit__"), [(unknown,)] * 3, {}))
            else:
                results.append(owner)
        return choices(results) or (unknown,), exits

    def mutate(node, scope):
        external = bool(function_names(node.func, scope) & external_mutations)
        if not external and (not isinstance(node.func, ast.Attribute) or node.func.attr not in mutation_methods):
            return None
        receiver = node.args[0] if external and node.args else node.func.value
        owners = [owner for owner in value(receiver, scope) if isinstance(owner, (Sequence, Keywords))]
        if not owners:
            return None
        arguments = expanded_arguments(node.args[1:] if external else node.args, scope)
        return mutate_values(owners, "append" if external else node.func.attr, arguments, call_keywords(node.keywords, scope), scope)

    def mutate_values(owners, name, arguments, keywords, scope):
        nonlocal attribute_mutations
        results = []
        for owner in owners:
            if name == "copy":
                results.append(Sequence(owner.state["rows"] if owner.unique else sequence_rows(owner), unique=owner.unique) if isinstance(owner, Sequence) else Keywords(dict(owner.items)))
                continue
            attribute_mutations += 1
            observe(owner.state)
            if isinstance(owner, Keywords):
                if name == "clear":
                    owner.items.clear()
                elif name == "update":
                    for assigned in arguments[:1]:
                        updates = []
                        for mapping in assigned:
                            updates.extend((mapping,) if isinstance(mapping, Keywords) else [[]] if mapping == "" else iterable_rows((mapping,), scope))
                        alternatives = []
                        for update in updates:
                            items = owner.items if len(updates) == 1 else dict(owner.items)
                            if isinstance(update, Keywords):
                                update_mapping(items, update.items)
                            else:
                                for entry in update:
                                    pairs = iterable_rows((entry,), scope)
                                    possible = []
                                    for pair in pairs:
                                        alternative = items if len(pairs) == 1 else dict(items)
                                        if len(pair) == 2:
                                            key = pair[0] if isinstance(pair[0], (str, int)) else unknown
                                            put_mapping(alternative, key, (pair[1],))
                                        else:
                                            put_mapping(alternative, unknown, tuple(pair) or (unknown,))
                                        possible.append(alternative)
                                    if len(possible) > 1:
                                        merged = merge(*possible)
                                        items.clear()
                                        items.update(merged)
                            alternatives.append(items)
                        if len(alternatives) > 1:
                            merged = merge(*alternatives)
                            owner.items.clear()
                            owner.items.update(merged)
                    update_mapping(owner.items, keywords)
                elif arguments:
                    for key in arguments[0]:
                        if not isinstance(key, (str, int)):
                            results.extend(item for assigned in owner.items.values() for item in assigned)
                            default = arguments[1] if len(arguments) > 1 else (None,) if name == "setdefault" else (unknown,)
                            results.extend(default)
                            if name == "setdefault":
                                owner.items[unknown] = choices((*owner.items.get(unknown, ()), *default))
                            continue
                        if name == "setdefault":
                            results.extend(owner.items.setdefault(key, arguments[1] if len(arguments) > 1 else (None,)))
                        elif name == "pop":
                            results.extend(owner.items.pop(key, arguments[1] if len(arguments) > 1 else (unknown,)))
                continue
            if not owner.unique and name in {"append", "extend"} and arguments:
                suffixes = []
                for operands_row in combinations(arguments):
                    if name == "append":
                        suffixes.append([operands_row[0]])
                    else:
                        operand = operands_row[0]
                        if isinstance(operand, Sequence) and not operand.unique:
                            suffixes.extend(operand.state["rows"])
                        elif type(operand) in (list, tuple):
                            suffixes.append(list(operand))
                        else:
                            suffixes.append(list(iterate_values((operand,), scope)))
                owner.state["rows"] = (RowConcat(owner.state["rows"], suffixes),)
                continue
            rows = []
            for previous in owner.state["rows"] if owner.unique else sequence_rows(owner):
                operands = combinations(arguments) if arguments else [()]
                for operands_row in operands:
                    row = list(previous)
                    if name == "clear":
                        row.clear()
                    elif name in {"append", "add"} and operands_row:
                        if not owner.unique or not any(same(item, operands_row[0]) for item in row):
                            row.append(operands_row[0])
                    elif name == "insert" and len(operands_row) > 1:
                        row.insert(operands_row[0] if isinstance(operands_row[0], int) else len(row), operands_row[1])
                    elif name in {"extend", "update"} and operands_row:
                        extension = operands_row[0]
                        extensions = extension.state["rows"] if isinstance(extension, Sequence) and extension.unique else sequence_rows(extension)
                        if all(isinstance(extension, (list, tuple)) for extension in extensions):
                            rows.extend(row + list(extension) for extension in extensions)
                            continue
                        row.extend(iterate_values((operands_row[0],), scope))
                    elif name == "pop" and row:
                        if owner.unique and not operands_row:
                            results.extend(row)
                            rows.append(row[:-1])
                            if len(row) > 1:
                                rows.append([row[-1], *row[:-2]])
                            continue
                        if operands_row and not isinstance(operands_row[0], int):
                            results.extend(row)
                            rows.extend(row[:index] + row[index + 1:] for index in range(len(row)))
                            continue
                        index = operands_row[0] if operands_row and isinstance(operands_row[0], int) else -1
                        if -len(row) <= index < len(row):
                            results.append(row.pop(index))
                    elif name in {"remove", "discard"} and operands_row:
                        index = next((index for index, item in enumerate(row) if same(item, operands_row[0])), None)
                        if index is not None:
                            row.pop(index)
                    elif name == "reverse":
                        row.reverse()
                    elif name == "sort":
                        try:
                            row.sort()
                        except TypeError:
                            pass
                    rows.append(row)
            owner.state["rows"] = Sequence(rows, unique=True).state["rows"] if owner.unique else choices(rows)
        return choices(results) or (None,)

    def scan(node, scope):
        nonlocal found
        if isinstance(node, (ast.Await, ast.FormattedValue)) or isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            value(node, scope)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            # Attribute reads execute instance property getters even in data calls.
            value(node, scope)
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
            value(node.target, scope)
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, ast.GeneratorExp):
            value(node, scope)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)):
            local = comprehension(node, scope)
            if local is not None:
                if isinstance(node, ast.DictComp):
                    scan(node.key, local)
                    scan(node.value, local)
                else:
                    scan(node.elt, local)
            return
        if isinstance(node, ast.Call):
            names = function_names(node.func, scope)
            functions = value(node.func, scope)
            iterator_next = bool(names & {"next", "builtins.next"} and node.args and any(is_value_iterator(item) for item in value(node.args[0], scope)))
            display_call = bool(names & {"print", "builtins.print"})
            ordered_consumer = bool(len(node.args) == 1 and not node.keywords and (
                names & {"list", "tuple", "sorted"} or any(name.endswith(".join") for name in names)))
            conversion_call = bool(names & (text_conversions.keys() | {"format", "builtins.format"}) or isinstance(node.func, ast.Attribute) and node.func.attr == "format")
            if iterator_next or display_call or ordered_consumer or conversion_call or names & protocol_data_calls:
                value(node, scope)
            if (names & (external_mutations | async_names) or isinstance(node.func, ast.Attribute) and (node.func.attr in mutation_methods
                    or any(is_exit_stack(owner) for owner in value(node.func.value, scope)))):
                value(node, scope)
            # Unknown data callees still expand starred operands. Known launch
            # and wrapper paths expand them in their own actual invocation.
            if (any(isinstance(argument, ast.Starred) for argument in node.args)
                    and not names & launches and not display_call
                    and not any(isinstance(function, (Function, Bound, Partial, ast.ClassDef)) for function in functions)):
                expanded_arguments(node.args, scope)
            for function in names & launches:
                arguments = expanded_arguments(node.args, scope)
                commands = arguments[0] if arguments else None
                if commands is None:
                    parameter = "args" if function.startswith("subprocess.") else "cmd" if function == "os.popen" else "command"
                    commands = call_keywords(node.keywords, scope).get(parameter)
                if commands is not None:
                    for command in commands:
                        if isinstance(command, CompiledCode) and function in {"exec", "eval"}:
                            command = command.source
                        if function in {"exec", "eval"} and isinstance(command, str) and overflow not in command:
                            embedded = parsed_source(command, "eval" if function == "eval" else "exec")
                            if embedded is None:
                                found |= executable_value(command)
                                continue
                            if executing_sources.count(command) >= 2 or len(executing_sources) >= 64:
                                found |= executable_value(command)
                                continue
                            executing_sources.append(command)
                            try:
                                # exec with explicit globals has a distinct lexical
                                # scope, but module attributes remain dynamically patched.
                                local = Scope() if len(node.args) > 1 else scope
                                if function == "eval":
                                    scan(embedded.body, local)
                                else:
                                    walk(embedded.body, local)
                            finally:
                                executing_sources.pop()
                        else:
                            found |= executable_value(command)
            called = False
            for function in functions:
                if isinstance(function, ast.alias) and function.name in launches and function.name not in names:
                    called = True
                    invoke(function, scope, node.args, node.keywords)
                elif isinstance(function, (Function, Bound, Partial, Instance)) or isinstance(function, Descriptor) and function.kind == "staticmethod":
                    called = True
                    value(node, scope)
                elif isinstance(function, ast.ClassDef):
                    called = True
                    value(node, scope)
                elif isinstance(function, Registry):
                    called = True
                    for handler in function.handlers:
                        invoke(handler, scope)
            if not called:
                callbacks = []
                if node.args and "atexit.register" in names:
                    exit_callbacks.append(Partial(value(node.args[0], scope), [value(argument, scope) for argument in node.args[1:]], call_keywords(node.keywords, scope)))
                elif node.args and "atexit.unregister" in names:
                    functions = value(node.args[0], scope)
                    exit_callbacks[:] = [callback for callback in exit_callbacks if not any(function in callback.functions for function in functions)]
                elif node.args and any(name.endswith(".submit") for name in names):
                    callbacks.append((node.args[0], [value(argument, scope) for argument in node.args[1:]], call_keywords(node.keywords, scope)))
                elif len(node.args) > 1 and any(name.endswith(".map") for name in names):
                    callbacks.append((node.args[0], [iterated(argument, scope) or (unknown,) for argument in node.args[1:]], {}))
                for callback, arguments, keywords in callbacks:
                    for function in value(callback, scope):
                        invoke(function, scope, arguments, keywords, evaluated=True)
                if node.args and not iterator_next and not ordered_consumer and (names & {"list", "tuple", "set", "frozenset", "next", "sum", "any", "all", "sorted", "dict", "min", "max", "collections.deque", "contextlib.ExitStack.enter_context", "contextlib.AsyncExitStack.enter_context", "contextlib.AsyncExitStack.enter_async_context"} or any(name.endswith(".join") for name in names)):
                    iterated(node.args[0], scope)
                if isinstance(node.func, ast.Attribute) and node.func.attr in {"set_defaults", "add_command"}:
                    for registry in value(node.func.value, scope):
                        if isinstance(registry, Registry):
                            handlers = value(node.args[0], scope) if node.func.attr == "add_command" and node.args else call_keywords(node.keywords, scope).get("func", ())
                            registry.handlers.extend(handlers)
                receiver = value(node.func.value, scope) if isinstance(node.func, ast.Attribute) else ()
                modeled_method = isinstance(node.func, ast.Attribute) and (
                    node.func.attr in mutation_methods | {"keys", "values", "items", "get", "count", "index"}
                    and any(isinstance(owner, (Sequence, Keywords)) for owner in receiver)
                    or any(is_exit_stack(owner) or isinstance(owner, (Registry, Deferred)) for owner in receiver)
                    or all(isinstance(owner, str) for owner in receiver) and bool(receiver))
                if not (names & (launches | pure_data_calls | external_mutations) or callbacks or modeled_method):
                    arguments = [*expanded_arguments(node.args, scope), *call_keywords(node.keywords, scope).values(), receiver]
                    if tuple(deferred_operands(arguments)):
                        value(node, scope)
                    escape_mutables(arguments)

        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.stmt):
                scan(child, scope)

    def observe_scope(owner):
        # A callback can mutate a captured scope outside the caller's parents.
        if branch_scopes:
            current, parents = branch_scopes[-1]
            if owner is not current and owner not in parents:
                parents[owner] = owner.state
                owner.state = dict(owner.state)

    def observe(mapping):
        """A branch saves only mutable mappings it actually changes."""
        if branch_changes:
            changes = branch_changes[-1]
            identity = id(mapping)
            if identity not in changes:
                changes[identity] = mapping, dict(mapping)

    class Branch(dict):
        def __init__(self, state, parents, attributes):
            super().__init__(state)
            self.parents, self.attributes = parents, attributes

    def merge(*states):
        def merged(alternatives):
            return {name: choices(item for state in alternatives for item in state.get(name, (unknown,)))
                    for name in set().union(*(state.keys() for state in alternatives))}
        owners = set().union(*(getattr(state, "parents", {}).keys() for state in states))
        for owner in owners:
            observe_scope(owner)
            owner.state = merged([getattr(state, "parents", {}).get(owner, owner.state) for state in states])
        attributes = set().union(*(getattr(state, "attributes", {}).keys() for state in states))
        for identity in attributes:
            target = next(state.attributes[identity][0] for state in states
                          if identity in getattr(state, "attributes", {}))
            updates = merged([getattr(state, "attributes", {}).get(identity, (target, target))[1] for state in states])
            observe(target)
            target.clear()
            target.update(updates)
        return merged(states)

    def assign_attribute(owner, name, assigned, scope):
        nonlocal attribute_mutations
        if isinstance(owner, ast.alias):
            attribute_mutations += 1
            observe(patched_attributes)
            name = owner.name + "." + name
            patched_attributes[name] = assigned
            expression_values.clear()
        elif isinstance(owner, Instance):
            members = class_member(owner.kind, name)
            properties = [item for item in members if isinstance(item, Descriptor) and item.kind == "property"]
            if properties:
                for descriptor in properties:
                    for setter in descriptor.setter:
                        invoke(Bound(setter, owner), scope, [assigned], evaluated=True)
                if len(properties) == len(members):
                    return
            attribute_mutations += 1
            observe(owner.attributes)
            owner.attributes[name] = assigned
        elif isinstance(owner, ast.ClassDef):
            attribute_mutations += 1
            observe(class_bindings.setdefault(id(owner), {}))
            class_bindings.setdefault(id(owner), {})[name] = assigned
        expression_values.clear()

    def delete_attribute(owner, name, scope):
        nonlocal attribute_mutations
        if isinstance(owner, Instance):
            members = class_member(owner.kind, name)
            properties = [item for item in members if isinstance(item, Descriptor) and item.kind == "property"]
            for descriptor in properties:
                for deleter in descriptor.deleter:
                    invoke(Bound(deleter, owner), scope, evaluated=True)
            if properties and len(properties) == len(members):
                expression_values.clear()
                return
            mapping = owner.attributes
        elif isinstance(owner, ast.ClassDef):
            mapping = class_bindings.setdefault(id(owner), {})
        elif isinstance(owner, ast.alias):
            mapping, name = patched_attributes, owner.name + "." + name
        else:
            return
        attribute_mutations += 1
        observe(mapping)
        mapping.pop(name, None)
        expression_values.clear()

    def bind(target, assigned, scope):
        nonlocal attribute_mutations
        if isinstance(target, ast.Name):
            scope[target.id] = assigned
        elif isinstance(target, ast.Attribute):
            for owner in value(target.value, scope):
                assign_attribute(owner, target.attr, assigned, scope)
        elif isinstance(target, ast.Subscript):
            for owner in value(target.value, scope):
                for key in value(target.slice, scope):
                    if isinstance(owner, Keywords):
                        attribute_mutations += 1
                        observe(owner.state)
                        if isinstance(key, (str, int)):
                            owner.items[key] = assigned
                        else:
                            owner.items[unknown] = choices((*owner.items.get(unknown, ()), *assigned))
                    elif isinstance(owner, Sequence):
                        attribute_mutations += 1
                        observe(owner.state)
                        rows = []
                        for row in sequence_rows(owner):
                            for item in assigned:
                                updated = list(row)
                                if isinstance(key, int) and -len(updated) <= key < len(updated):
                                    updated[key] = item
                                else:
                                    updated.append(item)
                                rows.append(updated)
                        owner.state["rows"] = choices(rows)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for index, child in enumerate(target.elts):
                bind(child, choices(item[index] if isinstance(item, (list, tuple)) and len(item) == len(target.elts) else unknown
                                    for entry in assigned for item in sequence_rows(entry)), scope)

    def branch(statements, scope, initial=None, executor=None):
        previous = scope.state
        scope.state = dict(previous if initial is None else initial)
        parents, owner = {}, scope.parent
        while owner is not None:
            parents[owner] = owner.state
            owner.state = dict(getattr(initial, "parents", {}).get(owner, owner.state))
            owner = owner.parent
        for captured in getattr(initial, "parents", {}):
            if captured is not scope and captured not in parents:
                parents[captured] = captured.state
                captured.state = dict(initial.parents[captured])
        changes = {}
        branch_changes.append(changes)
        branch_scopes.append((scope, parents))
        try:
            for target, assigned in getattr(initial, "attributes", {}).values():
                observe(target)
                target.clear()
                target.update(assigned)
            if executor is None:
                walk(statements, scope)
            else:
                executor(scope)
            return Branch(scope.state, {owner: owner.state for owner in parents},
                          {identity: (target, dict(target)) for identity, (target, _) in changes.items()})
        finally:
            branch_scopes.pop()
            branch_changes.pop()
            scope.state = previous
            for owner, state in parents.items():
                owner.state = state
            for target, previous_state in changes.values():
                target.clear()
                target.update(previous_state)

    descriptor_names = {"classmethod", "staticmethod", "property", "builtins.classmethod", "builtins.staticmethod", "builtins.property", "functools.cached_property"}

    def apply_decorators(decorated, expressions, decorators, scope):
        for expression, assigned in reversed(list(zip(expressions, decorators))):
            call = expression.func if isinstance(expression, ast.Call) else expression
            names = function_names(call, scope)
            parents = [registry for registry in value(call.value, scope) if isinstance(registry, Registry)] if isinstance(call, ast.Attribute) and call.attr in {"command", "group"} else []
            if names & descriptor_names:
                decorated = tuple(Descriptor(function, name.rsplit(".", 1)[-1]) for name in names & descriptor_names for function in decorated)
            elif parents:
                if call.attr == "group":
                    child = Registry()
                    child.handlers.extend(decorated)
                    decorated = (child,)
                for registry in parents:
                    registry.handlers.extend(decorated)
            elif "click.group" in names:
                registry = Registry()
                registry.handlers.extend(decorated)
                decorated = (registry,)
            elif "atexit.register" in names:
                exit_callbacks.append(Partial(decorated, [], {}))
            elif any(isinstance(wrapper, PatchContext) for wrapper in assigned):
                patches = [wrapper for wrapper in assigned if isinstance(wrapper, PatchContext)]
                replacements = []
                for function in decorated:
                    if isinstance(function, ast.ClassDef):
                        bindings = class_bindings[id(function)]
                        prefixes = patched_attributes.get("unittest.mock.patch.TEST_PREFIX", ("test",))
                        for name in class_names(function):
                            if not any(name.startswith(prefix) for prefix in prefixes if isinstance(prefix, str)):
                                continue
                            callbacks = class_member(function, name)
                            replacements_for_name = []
                            for callback in callbacks:
                                for wrapper in patches:
                                    if isinstance(callback, Descriptor) and callback.kind in {"staticmethod", "classmethod"}:
                                        replacements_for_name.append(Descriptor(Bound(wrapper, (callback.function,)), callback.kind))
                                    elif isinstance(callback, (Function, Bound)):
                                        replacements_for_name.append(Bound(wrapper, (callback,)))
                                    else:
                                        replacements_for_name.append(callback)
                            bindings[name] = choices(replacements_for_name)
                        replacements.append(function)
                    else:
                        replacements.extend(Bound(wrapper, (function,)) for wrapper in patches)
                decorated = choices(replacements)
            else:
                replacements = choices(item for wrapper in assigned if isinstance(wrapper, (Function, Bound, Partial, Descriptor, Instance, ast.ClassDef))
                                       for item in invoke(wrapper, scope, [decorated], evaluated=True))
                # An unmodeled wrapper can still call its input later. Preserve
                # that possibility without eagerly invoking the original body.
                if not replacements or any(item is unknown or isinstance(item, ast.alias) for item in replacements):
                    replacements = choices((*replacements, *decorated))
                decorated = replacements
        return decorated

    class InexactDataLoop(Exception):
        pass

    def data_conversion(operation, literal):
        try:
            return operation(literal)
        except (ValueError, TypeError, OverflowError):
            raise InexactDataLoop() from None

    def data_literal(item, seen=()):
        if unresolved_text(item):
            raise InexactDataLoop()
        if type(item) in (str, bytes, int, float, complex, bool, type(None)):
            return item
        if id(item) in seen:
            raise InexactDataLoop()
        descendants = (*seen, id(item))
        if isinstance(item, Sequence) and not item.unique:
            rows = item.state["rows"]
            def exact_row(row):
                result, pending = [], [row]
                while pending:
                    row = pending.pop()
                    if isinstance(row, RowConcat):
                        if len(row.left) != 1 or len(row.right) != 1:
                            raise InexactDataLoop()
                        pending.extend((row.right[0], row.left[0]))
                    elif type(row) in (list, tuple):
                        result.extend(row)
                    else:
                        raise InexactDataLoop()
                return result
            if len(rows) == 1:
                return [data_literal(child, descendants) for child in exact_row(rows[0])]
        elif type(item) in (list, tuple):
            return type(item)(data_literal(child, descendants) for child in item)
        elif isinstance(item, Keywords):
            result = {}
            for key, assigned in item.items.items():
                if type(key) not in (str, bytes, int, float, complex, bool, type(None), tuple) or len(assigned) != 1:
                    raise InexactDataLoop()
                literal_key = data_literal(key, descendants)
                try:
                    hash(literal_key)
                except TypeError:
                    raise InexactDataLoop() from None
                result[literal_key] = data_literal(assigned[0], descendants)
            return result
        raise InexactDataLoop()

    def data_expression(node, scope):
        if isinstance(node, ast.Constant):
            data_literal(node.value)
            return node.value
        if isinstance(node, ast.Name):
            assigned = scope.get(node.id, ())
            if len(assigned) != 1:
                raise InexactDataLoop()
            data_literal(assigned[0])
            return assigned[0]
        if isinstance(node, (ast.List, ast.Tuple)):
            items = [data_expression(child, scope) for child in node.elts]
            return Sequence((items,)) if isinstance(node, ast.List) else tuple(items)
        if isinstance(node, ast.Dict) and all(key is not None for key in node.keys):
            result = {}
            for key, child in zip(node.keys, node.values):
                key = data_expression(key, scope)
                if type(key) not in (str, int):
                    raise InexactDataLoop()
                result[key] = (data_expression(child, scope),)
            return Keywords(result)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for child in node.values:
                if isinstance(child, ast.Constant) and type(child.value) is str:
                    parts.append(child.value)
                elif isinstance(child, ast.FormattedValue) and child.format_spec is None:
                    literal = data_literal(data_expression(child.value, scope))
                    operation = {-1: builtins.str, 115: builtins.str, 114: builtins.repr, 97: builtins.ascii}.get(child.conversion)
                    if operation is None:
                        raise InexactDataLoop()
                    parts.append(data_conversion(operation, literal))
                else:
                    raise InexactDataLoop()
            return "".join(parts)
        if isinstance(node, ast.IfExp):
            condition = data_literal(data_expression(node.test, scope))
            if type(condition) not in (str, bytes, int, float, complex, bool, type(None)):
                raise InexactDataLoop()
            return data_expression(node.body if condition else node.orelse, scope)
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            left, right = (data_literal(data_expression(child, scope)) for child in (node.left, node.comparators[0]))
            if type(left) not in (str, bytes, int, float, complex, bool, type(None)) or type(right) not in (str, bytes, int, float, complex, bool, type(None)):
                raise InexactDataLoop()
            return left == right if isinstance(node.ops[0], ast.Eq) else left != right
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = data_expression(node.left, scope), data_expression(node.right, scope)
            first, second = data_literal(left), data_literal(right)
            if type(first) is not type(second) or type(first) not in (str, bytes, list, tuple):
                raise InexactDataLoop()
            if type(first) is list:
                return Sequence(([*sequence_rows(left)[0], *sequence_rows(right)[0]],))
            if type(first) is tuple:
                return left + right
            return first + second
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1 and not node.keywords:
            assigned = scope.get(node.func.id)
            if assigned is None:
                name = node.func.id
                if "builtins." + name in patched_attributes:
                    raise InexactDataLoop()
            elif len(assigned) == 1 and isinstance(assigned[0], ast.alias) and assigned[0].name in {
                    "repr", "str", "ascii", "builtins.repr", "builtins.str", "builtins.ascii"}:
                name = assigned[0].name.rsplit(".", 1)[-1]
            else:
                raise InexactDataLoop()
            operation = {"repr": builtins.repr, "str": builtins.str, "ascii": builtins.ascii}.get(name)
            if operation is not None:
                return data_conversion(operation, data_literal(data_expression(node.args[0], scope)))
        raise InexactDataLoop()

    def data_target(target, item):
        if isinstance(target, ast.Name):
            return
        if isinstance(target, (ast.List, ast.Tuple)):
            row = sequence_rows(item)[0] if isinstance(item, Sequence) and len(item.state["rows"]) == 1 else item
            if type(row) in (list, tuple) and len(row) == len(target.elts):
                for child, value in zip(target.elts, row):
                    data_target(child, value)
                return
        raise InexactDataLoop()

    def data_join_dag(argument, separator, scope):
        if type(separator) is not str:
            return None
        try:
            data_literal(separator)
            mapper = None
            if isinstance(argument, Generator) and isinstance(argument.node, ast.GeneratorExp) and len(argument.node.generators) == 1:
                if argument.state["exhausted"] == (True,):
                    return ("",)
                if True in argument.state["partial"]:
                    return None
                generator = argument.node.generators[0]
                if generator.ifs or len(argument.arguments[0]) != 1:
                    return None
                source = argument.arguments[0][0]
                def mapper(item):
                    data_target(generator.target, item)
                    local = Scope(argument.scope)
                    bind(generator.target, (item,), local)
                    return data_literal(data_expression(argument.node.elt, local))
            else:
                source = argument
                mapper = data_literal
            if not isinstance(source, Sequence) or not any(isinstance(row, RowConcat) for row in source.state["rows"]):
                return None
            def alternatives(rendered):
                outputs = {False: [], True: []}
                for values in rendered:
                    for empty, items in values.items():
                        outputs[empty].extend(items)
                return {empty: choices(values) for empty, values in outputs.items()}
            def concatenate(left, right):
                merged = {False: [], True: []}
                for first_empty, first in left.items():
                    for second_empty, second in right.items():
                        if not first or not second:
                            continue
                        prefix = add(first, (separator,)) if not first_empty and not second_empty else first
                        merged[first_empty and second_empty].extend(add(prefix, second))
                return {empty: choices(values) for empty, values in merged.items()}
            def leaf(row):
                values = [mapper(item) for item in row]
                if any(type(item) is not str for item in values):
                    raise InexactDataLoop()
                return {not values: (separator.join(values),)}
            rendered = alternatives(row_dag_values(source.state["rows"], leaf, concatenate, alternatives))
            return choices(item for values in rendered.values() for item in values)
        except InexactDataLoop:
            return None

    def data_statement(statement, scope, source):
        if isinstance(statement, ast.Assign) and all(isinstance(target, ast.Name) for target in statement.targets):
            assigned = data_expression(statement.value, scope)
            for target in statement.targets:
                bind(target, (assigned,), scope)
            return
        if isinstance(statement, ast.AugAssign) and isinstance(statement.target, ast.Name) and isinstance(statement.op, ast.Add):
            owners = scope.get(statement.target.id, ())
            extension = data_expression(statement.value, scope)
            if (len(owners) == 1 and isinstance(owners[0], Sequence) and not owners[0].unique and owners[0] is not source and len(owners[0].state["rows"]) == 1
                    and isinstance(extension, (Sequence, list, tuple))):
                data_literal(extension)
                mutate_values(owners, "extend", [(extension,)], {}, scope)
                return
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and not call.keywords:
                owners = scope.get(call.func.value.id, ())
                if len(owners) != 1 or not isinstance(owners[0], Sequence) or owners[0].unique or owners[0] is source or len(owners[0].state["rows"]) != 1:
                    raise InexactDataLoop()
                if call.func.attr in {"clear", "reverse"} and not call.args:
                    mutate_values(owners, call.func.attr, [], {}, scope)
                    return
                if call.func.attr in {"append", "extend"} and len(call.args) == 1:
                    operand = data_expression(call.args[0], scope)
                    if call.func.attr == "extend" and not isinstance(operand, (Sequence, list, tuple)):
                        raise InexactDataLoop()
                    data_literal(operand)
                    mutate_values(owners, call.func.attr, [(operand,)], {}, scope)
                    return
            data_expression(call, scope)
            return
        if isinstance(statement, ast.If):
            condition = data_literal(data_expression(statement.test, scope))
            if type(condition) not in (str, bytes, int, float, complex, bool, type(None)):
                raise InexactDataLoop()
            for child in statement.body if condition else statement.orelse:
                data_statement(child, scope, source)
            return
        if isinstance(statement, ast.Pass):
            return
        raise InexactDataLoop()

    def walk(statements, scope, *, methods=False):
        nonlocal attribute_mutations
        for statement in statements:
            if isinstance(statement, ast.ClassDef):
                # Syntax may be reused by exec or a factory, but each class
                # creation has independent attributes and method provenance.
                statement = copy.deepcopy(statement)
                created_classes.append(statement)
            expression_values.clear()
            scan(statement, scope)
            if isinstance(statement, ast.Return) and return_values:
                return_values[-1].extend(value(statement.value, scope))
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, (ast.Yield, ast.YieldFrom)) and return_values:
                assigned = iterated(statement.value.value, scope) if isinstance(statement.value, ast.YieldFrom) else value(statement.value.value, scope)
                return_values[-1].extend(assigned)
                yield_values[-1][id(statement)] = choices((*yield_values[-1].get(id(statement), ()), *assigned))
            elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                assigned = value(statement.value, scope) if statement.value is not None else (unknown,)
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in targets:
                    if isinstance(statement, ast.AugAssign) and isinstance(statement.op, ast.Add) and isinstance(target, ast.Name):
                        previous = scope.get(target.id, (unknown,))
                        combined = []
                        for owner in previous:
                            if isinstance(owner, Sequence):
                                attribute_mutations += 1
                                observe(owner.state)
                                owner.state["rows"] = add((owner,), assigned)
                                combined.append(owner)
                            else:
                                combined.extend(add((owner,), assigned))
                        assigned = choices(combined)
                    if isinstance(statement, ast.AugAssign) and isinstance(statement.op, ast.Add) and isinstance(target, ast.Attribute):
                        assigned = add(value(target, scope), assigned)
                    bind(target, assigned, scope)
            elif isinstance(statement, ast.Delete):
                for target in statement.targets:
                    if isinstance(target, ast.Attribute):
                        for owner in value(target.value, scope):
                            delete_attribute(owner, target.attr, scope)
            elif isinstance(statement, ast.Import):
                for imported in statement.names:
                    scope[imported.asname or imported.name.split(".")[0]] = (ast.alias(name=imported.name if imported.asname else imported.name.split(".")[0]),)
            elif isinstance(statement, ast.ImportFrom):
                for imported in statement.names:
                    target = (statement.module or "") + "." + imported.name
                    scope[imported.asname or imported.name] = patched_attributes.get(target, (ast.alias(name=target),))
            elif isinstance(statement, ast.If):
                bodies = (statement.body, statement.orelse)
                if isinstance(statement.test, ast.Constant):
                    bodies = (bodies[0] if statement.test.value else bodies[1],)
                scope.state = merge(*(branch(body, scope) for body in bodies))
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                if isinstance(statement, ast.For):
                    assigned = value(statement.iter, scope)
                    source = assigned[0] if len(assigned) == 1 else None
                    rows = source.state["rows"] if isinstance(source, Sequence) and not source.unique else (source,) if type(source) in (list, tuple) else ()
                    if len(rows) == 1 and type(rows[0]) in (list, tuple):
                        original_rows = source.state["rows"] if isinstance(source, Sequence) else None
                        def run_exact(local):
                            for item in rows[0]:
                                data_literal(item)
                                data_target(statement.target, item)
                                bind(statement.target, (item,), local)
                                for child in statement.body:
                                    data_statement(child, local, source)
                                    if isinstance(source, Sequence) and source.state["rows"] is not original_rows:
                                        raise InexactDataLoop()
                        try:
                            exact = branch((), scope, executor=run_exact)
                        except InexactDataLoop:
                            pass
                        else:
                            scope.state = merge(exact)
                            walk(statement.orelse, scope)
                            continue
                iteration = dict(scope.state)
                if isinstance(statement, (ast.For, ast.AsyncFor)):
                    items = iterated(statement.iter, scope)
                    if not items:
                        walk(statement.orelse, scope)
                        continue
                    previous = scope.state
                    scope.state = iteration
                    bind(statement.target, items, scope)
                    iteration, scope.state = scope.state, previous
                repeated = merge(scope.state, branch(statement.body, scope, iteration))
                if isinstance(statement, (ast.For, ast.AsyncFor)):
                    captured = assigned if isinstance(statement, ast.For) else ()
                    def repeat_body(local):
                        known = [item for item in captured if isinstance(item, (Sequence, list, tuple))]
                        opaque = not captured or len(known) != len(captured)
                        current = choices((*iterate_values(known, local), *(items if opaque else ())))
                        if current:
                            bind(statement.target, current, local)
                            walk(statement.body, local)
                    repeated = merge(repeated, branch((), scope, repeated, executor=repeat_body))
                else:
                    repeated = merge(repeated, branch(statement.body, scope, repeated))
                scope.state = repeated
                walk(statement.orelse, scope)
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                normal = branch(statement.body, scope)
                exceptional = merge(scope.state, normal)
                alternatives = [branch(statement.orelse, scope, normal)]
                alternatives.extend(branch(handler.body, scope, exceptional) for handler in statement.handlers)
                scope.state = merge(*alternatives)
                walk(statement.finalbody, scope)
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                exits = []
                for item in statement.items:
                    assigned = value(item.context_expr, scope)
                    assigned, callbacks = enter_context(assigned, scope, async_=isinstance(statement, ast.AsyncWith))
                    exits.extend(callbacks)
                    if item.optional_vars is not None:
                        bind(item.optional_vars, assigned, scope)
                walk(statement.body, scope)
                for callback in reversed(exits):
                    if isinstance(callback, PatchContext) or is_exit_stack(callback):
                        close_context(callback, scope)
                    else:
                        invoke(callback, scope, evaluated=True)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorators = [value(decorator, scope) for decorator in statement.decorator_list]
                function = Function(statement, scope.parent if methods else scope)
                scope[statement.name] = (function,)
                scope[statement.name] = apply_decorators((function,), statement.decorator_list, decorators, scope)
                if partial:
                    invoke(function, scope)
            elif isinstance(statement, ast.ClassDef):
                decorators = [value(decorator, scope) for decorator in statement.decorator_list]
                scope[statement.name] = (statement,)
                local = Scope(scope)
                walk(statement.body, local, methods=True)
                class_bindings[id(statement)] = local.state
                for assigned in local.state.values():
                    for callback in assigned:
                        for function in wrapped_functions(callback):
                            method_classes[id(function.node)] = statement
                class_bases[id(statement)] = choices(base for expression in statement.bases for base in value(expression, scope) if isinstance(base, (ast.ClassDef, ast.alias)))
                scope[statement.name] = apply_decorators((statement,), statement.decorator_list, decorators, scope)
            elif isinstance(statement, ast.Match):
                scope.state = merge(scope.state, *(branch(case.body, scope) for case in statement.cases))
        return scope.state

    walk(tree.body, Scope())
    for callback in reversed(exit_callbacks):
        invoke(callback, Scope(), evaluated=True)
    return found

def python_fragment_ownership(content, *, partial=True, python_check=None):
    """Partial Python edits retain Python string boundaries, never shell syntax."""
    classify = python_check or python_ownership
    try:
        ast.parse(textwrap.dedent(content))
    except SyntaxError:
        pass
    else:
        return classify(content, partial=partial)
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
            if classify(expression):
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
                        if classify(definition + "\n" + expression):
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


def executable_file_content(path, content, *, partial=False, python_check=None):
    if isinstance(content, UnresolvedStream):
        return content.possible_ownership
    checker = python_fragment_ownership if python_check is None else python_check
    return checker(content, partial=partial) if Path(path).suffix == ".py" else executable_content(content)


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
        sources = (value, *javascript_generated_sources(value))
        capabilities = any(javascript_capabilities(javascript_projection(current)[1], current) for current in sources)
        for current in sources:
            for _, raw in composed_patch_fields(current):
                try:
                    patches.append(composed_string(raw, current))
                except (SyntaxError, ValueError):
                    pass
            for _, patch in javascript_possible_patches(current, capabilities=capabilities):
                for effect in native_file_effects("apply_patch", patch):
                    for content in effect["contents"]:
                        yield effect["path"], content
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
                    call = (identifier, method, args, native_cwd(args.get("workdir") or args.get("cwd") or args.get("working_directory"), cwd))
                    yield (*call, record.get("timestamp")) if with_timestamp else call
                continue
            args = arguments(value)
            call = (identifier, name, value, native_cwd(args.get("workdir") or args.get("cwd") or args.get("working_directory"), cwd) if isinstance(args, dict) else cwd)
            yield (*call, record.get("timestamp")) if with_timestamp else call


def native_path(path, cwd):
    if posixpath.isabs(path):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(cwd, path)) if cwd else None


def native_cwd(value, cwd):
    """Native tools resolve a relative override from the recorded session cwd."""
    return native_path(value, cwd) if value else cwd


def native_file_effects(name, value):
    """Keep native patch and shell effects in their visible call order."""
    native = arguments(value)
    if name == "Edit" and isinstance(native, dict):
        yield {"kind": "write", "path": native.get("file_path", ""),
               "contents": (native.get("new_string", ""),), "replaces": False,
               "updates": ((native.get("old_string", ""), native.get("new_string", ""),
                            -1 if native.get("replace_all") else 1),)}
        return
    if name in {"apply_patch", "functions.apply_patch"} and isinstance(native, str):
        for match in re.finditer(r"(?m)^\*\*\* (Add|Update) File: (.+)\n([\s\S]*?)(?=^\*\*\* |\Z)", native):
            updates, contents = [], []
            for hunk in re.split(r"(?m)^@@[^\n]*\n", match[3]):
                lines = hunk.splitlines()
                old = "\n".join(line[1:] for line in lines if line.startswith(("-", " ")))
                new = "\n".join(line[1:] for line in lines if line.startswith(("+", " ")))
                if new or old:
                    contents.append(new + "\n" if match[1] == "Add" else new)
                    updates.append((old, new, 1))
            if contents:
                effect = {"kind": "write", "path": match[2], "contents": tuple(contents),
                          "replaces": match[1] == "Add"}
                if match[1] == "Update":
                    effect["updates"] = tuple(updates)
                yield effect
        return
    if name in {"exec", "functions.exec"} and isinstance(native, str):
        sources = (native, *javascript_generated_sources(native))
        capabilities = any(javascript_capabilities(javascript_projection(current)[1], current) for current in sources)
        def direct_effects(current):
            patches = list(composed_patch_fields(current))
            calls = [(position, "apply_patch", raw, None, None) for position, raw in patches]
            for _, patch in javascript_possible_patches(current, capabilities=capabilities):
                for effect in native_file_effects("apply_patch", patch):
                    yield dict(effect, replaces=False, partial=True)
            for match in composed_shell_fields(current):
                opening = current.find("{", match.start())
                literal = re.match(rf'''\{{(?:{JS_STRING}|[^{{}}"'`])*\}}''', current[opening:])
                parsed = composed_calls("text(await tools.exec_command(" + literal[0] + "))") if literal else []
                args = parsed[0][1] if parsed else {}
                calls.append((match.start(), "exec_command", match[1], args.get("workdir") or args.get("cwd"), args.get("shell")))
            for _, method, raw, cwd, shell in sorted(calls):
                try:
                    content = composed_string(raw, current)
                except (SyntaxError, ValueError):
                    continue
                for effect in native_file_effects(method, {"cmd": content, "shell": shell} if method == "exec_command" else content):
                    yield dict(effect, cwd=cwd) if cwd else effect
        direct = list(direct_effects(native))
        generated = [effect for current in sources[1:] for effect in direct_effects(current)]
        if generated:
            # Source calls are discovered without evaluating JS control flow.
            # Keep all full-source writes as alternatives before any possible
            # execution, so opaque interleavings cannot erase old ownership.
            for effect in (*direct, *generated):
                if effect["kind"] == "write":
                    yield dict(effect, replaces=False, partial=effect.get("partial", not effect["replaces"]))
            for effect in (*direct, *generated):
                if effect["kind"] != "write":
                    yield effect
            return
        if direct:
            yield from direct
            return
    texts = list(shell_texts("exec_command" if name == "functions.exec_command" else name, value))
    if texts:
        for text in texts:
            effects = []
            try:
                shell_code(text, effects=effects, source_shell=native.get("shell") if name in {"exec_command", "functions.exec_command"} and isinstance(native, dict) else None)
            except ValueError:
                pass  # Typed ownership retains the effects collected first.
            yield from effects
        return
    replacements, grouped = set(), {}
    for path, content in file_writes(name, value, replacements=replacements):
        grouped.setdefault(path, []).append(content)
    for path, contents in grouped.items():
        yield {"kind": "write", "path": path, "contents": tuple(contents), "replaces": path in replacements}


def file_source_context(effect, previous):
    """Compose only recorded Python bytes, retaining alternatives for uncertain edits."""
    definite = previous.get("definite", False)
    erasure_exact = previous.get("erasure_exact", definite)
    previous = previous.get("contents", ())
    contents = tuple(content for content in effect["contents"] if isinstance(content, str))
    full = not effect.get("partial", not effect["replaces"])
    exact = definite and bool(previous) and "updates" in effect
    if full:
        result = contents if effect["replaces"] else (*previous, *contents)
        definite |= effect["replaces"]
    elif "updates" in effect:
        result = []
        for source in previous:
            if isinstance(source, UnresolvedStream):
                continue
            updated = source
            for old, new, count in effect["updates"]:
                if old and old in updated:
                    if count != -1 and updated.count(old) != 1:
                        exact = False
                        definite = False
                    updated = updated.replace(old, new, count)
                else:
                    exact = False
                    definite = False
                    # A context-free insertion can append a caller. A hunk
                    # with unavailable context retains that possibility too.
                    updated += "\n" + new
            result.append(updated)
    else:
        result = [source + content for source in previous if not isinstance(source, UnresolvedStream)
                  for content in contents]
    if not full:
        for source in previous:
            if isinstance(source, UnresolvedStream):
                exact = False
                result.append(source)
    # Alternatives are real recorded writes, not values fabricated by the
    # analyzer. Keep them isolated: joining programs lets a later definition
    # shadow a reachable owning definition from a different native write.
    # Ordinary source composition can prove dormant code remains inert even
    # when a native outcome cannot certify erasure. Keep that precision apart
    # from the stronger completed-update proof, so uncertain edits cannot
    # retire a previously owning path.
    erasure_exact = (bool(effect["replaces"]) and effect.get("erasure_exact", True) if full else erasure_exact and exact)
    erasure_exact &= effect.get("partial") is not True
    return {"contents": tuple(dict.fromkeys(result)), "definite": definite,
            "erasure_exact": erasure_exact}, exact


def apply_file_effect(effect, cwd, candidates, sources=None, *, python_checks=None, source_ready=None):
    if effect["kind"] != "write":
        return
    path = native_path(effect["path"], native_cwd(effect.get("cwd"), cwd)) if effect["path"] else None
    if path:
        if effect.get("invalidates"):
            if effect.get("copy_from"):
                source = native_path(effect["copy_from"], native_cwd(effect.get("cwd"), cwd))
                if source is None:
                    return False
                previous_sources = dict(sources or {})
                previous_candidates = set(candidates)
                known = set(previous_sources) | previous_candidates
                source_paths = [item for item in known if item == source or item.startswith(source.rstrip('/') + '/')]
                if not source_paths:
                    source_paths = [source]
                directories = effect.get("destination_is_directory") or effect.get("destination_may_be_directory") and (
                    effect["path"].endswith('/') or effect["path"] in {".", ".."}
                    or any(item.startswith(path.rstrip('/') + '/') and previous_sources.get(item, {}).get("definite")
                           for item in known))
                roots = [(posixpath.join(path, posixpath.basename(source)) if directories and not effect.get("copy_contents") else path, False)]
                if effect.get("destination_may_be_directory") and not directories and not effect.get("copy_contents"):
                    roots.append((posixpath.join(path, posixpath.basename(source)), True))
                # Unknown source children may replace existing destination
                # descendants too. Retain their possible bytes without trusting
                # them as the exact base for a later partial edit.
                if sources is not None:
                    for root, _ in roots:
                        for existing in list(sources):
                            if existing == root or effect.get("copy_recursive") and existing.startswith(root.rstrip('/') + '/'):
                                counterpart = posixpath.normpath(posixpath.join(source, posixpath.relpath(existing, root)))
                                if existing not in source_paths or counterpart in known:
                                    sources[existing] = dict(sources[existing], definite=False)
                owning = False
                for original in source_paths:
                    relative = posixpath.relpath(original, source)
                    prior = previous_sources.get(original)
                    contents = prior.get("contents", ()) if prior else ()
                    certain = bool(prior and prior.get("definite")) or original in previous_candidates
                    ready = source_ready is None or source_ready(original)
                    for root, speculative in roots:
                        destination = posixpath.normpath(root if relative == "." else posixpath.join(root, relative))
                        transferred = {"kind": "write", "path": destination, "contents": contents,
                                       "replaces": effect["replaces"] and certain and ready and not speculative, "partial": False,
                                       "erasure_exact": bool(prior and prior.get("erasure_exact", prior.get("definite", False))) and ready}
                        result = apply_file_effect(transferred, None, candidates, sources, python_checks=python_checks)
                        if not certain:
                            result |= destination in previous_candidates
                        if original in previous_candidates:
                            candidates.add(destination)
                            result = True
                        if sources is not None and (not certain or speculative) and destination in sources:
                            sources[destination] = dict(sources[destination], definite=False)
                        owning |= bool(result)
                return owning
            if sources is not None:
                for existing in list(sources):
                    if existing == path or effect.get("recursive") and existing.startswith(path.rstrip('/') + '/'):
                        sources[existing] = dict(sources[existing], definite=False)
            return False
        fragment_check, full_check = python_checks or (python_fragment_ownership, python_ownership)
        owning = any(executable_file_content(effect["path"], content, partial=effect.get("partial", not effect["replaces"]),
                                             python_check=fragment_check) for content in effect["contents"])
        if sources is not None and Path(path).suffix == ".py":
            sources[path], exact = file_source_context(effect, sources.get(path, {}))
            full = not effect.get("partial", not effect["replaces"])
            contents = effect["contents"] if full else sources[path]["contents"]
            composed = any(content.possible_ownership if isinstance(content, UnresolvedStream)
                           else full_check(content) for content in contents)
            owning = composed if exact else owning or composed
        elif sources is not None:
            # A recorded harmless source can replace an owning destination.
            # An unknown or failed copy cannot prove its old bytes were erased.
            sources[path] = {"contents": (), "definite": bool(effect["replaces"])}
        if owning:
            candidates.add(path)
        elif effect["replaces"]:
            candidates.discard(path)
        return owning
    return False


def native_timestamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def python_source_checks():
    """Memoize immutable source classification for one collected native snapshot."""
    classifier, fragments = python_ownership, python_fragment_ownership
    @lru_cache(maxsize=None)
    def cached(content, partial):
        return classifier(content, partial=partial)
    def full(content, *, partial=False):
        return cached(content, partial)
    @lru_cache(maxsize=None)
    def fragment(content, *, partial=True):
        return fragments(content, partial=partial, python_check=full)
    return fragment, full


def native_write_outcomes(records, harness, identifiers):
    """Reuse native binding; an erasure needs affirmative successful output."""
    selected = {(name, id(value)) for record in records
                for identifier, name, value in calls_and_results(record, harness)[0] if identifier in identifiers}
    native_invocations = {identifier: (name, value) for record in records
                          for identifier, name, value in calls_and_results(record, harness)[0]}
    native_names = {identifier: name for identifier, (name, _) in native_invocations.items()}
    composed_results = serial_native_bindings(records) if harness == "codex" else {}
    ends, successful, failed, native_results = {}, set(), set(), {}
    for index, identifier, result in bound_outputs(records, harness, lambda name, value: (name, id(value)) in selected):
        if result.get("_result") and not result.get("_pending"):
            ends[identifier] = index
            native_results[identifier] = result.get("_tool_call_id")
        if harness == "codex":
            if not result.get("_result"):
                continue  # Recursively decoded stdout is never native status.
            output_call = native_invocations.get(result.get("_tool_call_id"))
            if output_call is None:
                continue
            output_name, output_value = output_call
            composed = output_name in {"exec", "functions.exec"}
            normalized = records[index].get("_native_serial_result") is _SERIAL_NATIVE_RESULT
            binding = composed_results.get((index, result.get("_tool_call_id")))
            serial, wrappers = [], None
            if binding is not None:
                _, serial, wrappers = binding
            elif normalized and output_name not in {"apply_patch", "functions.apply_patch"}:
                serial = [(output_name, arguments(output_value), False)]
                blocks, _ = serial_native_chunk(records[index])
                wrappers = serial_native_results(blocks, serial)
            if wrappers is not None:
                statuses = [wrapper.get("exit_code") if wrapper is not None else None
                            for (method, args, stdout), wrapper in zip(serial, wrappers)
                            if normalized or method in {"write_stdin", "wait"}
                            or any(effect["kind"] == "write" for effect in native_file_effects(method, args))]
                if statuses and all(type(status) is int for status in statuses):
                    (successful if all(status == 0 for status in statuses) else failed).add(identifier)
            elif not composed:
                native = (records[index].get("payload") or {}).get("output")
                match = re.match(r"\AExit code: (-?\d+)\nWall time: [^\n]+\nOutput:\n", native) if isinstance(native, str) else None
                if match:
                    (successful if int(match[1]) == 0 else failed).add(identifier)
            continue
        if harness == "claude" and native_names.get(identifier) in {"Write", "Edit", "Bash", "apply_patch", "functions.apply_patch"}:
            continue  # Native is_error owns status; content is only stdout.
        if result.get("_tool_error"):
            failed.add(identifier)
        elif type(result.get("exit_code")) is int and result["exit_code"] != 0:
            failed.add(identifier)
        elif not result.get("operation") and type(result.get("exit_code")) is int and result["exit_code"] == 0:
            successful.add(identifier)
    if harness == "claude":
        for identifier, index in ends.items():
            if native_names.get(identifier) not in {"Write", "Edit", "Bash", "apply_patch", "functions.apply_patch"}:
                continue
            for block in (records[index].get("message") or {}).get("content", ()):
                if (isinstance(block, dict) and block.get("type") == "tool_result"
                        and block.get("tool_use_id") == native_results.get(identifier)):
                    if block.get("is_error", False) is False and isinstance(block.get("content"), (str, list)):
                        successful.add(identifier)
                    else:
                        failed.add(identifier)
    return ends, successful - failed


def completed_python_update(name, path, context, source, *, python_checks=None):
    """A finished native exact edit may retire its own recorded old bytes.

    Computed JS writes and explicit partial alternatives cannot prove erasure.
    The returned full-source state is used only by native completion replay;
    the writer's original effects and interval remain live until that bound.
    """
    if (name not in {"apply_patch", "functions.apply_patch", "Edit"}
            or Path(path).suffix != ".py" or not source.get("definite") or not source.get("erasure_exact")
            or not any("updates" in effect for effect in context)
            or any(effect.get("partial") is True or effect.get("invalidates") for effect in context)):
        return None
    contents = source.get("contents", ())
    if not contents or any(not isinstance(content, str) for content in contents):
        return None
    full_check = python_checks[1] if python_checks else python_ownership
    if any(full_check(content) for content in contents):
        return None
    return ({"kind": "write", "path": path, "contents": tuple(contents),
             "replaces": True, "partial": False},)


def ownership_file_candidates(histories, *, python_checks=None):
    """Preserve each peer write's native interval, not its eventual contents.

    First-record identity excludes a history's own future writes. A writer is
    possible from launch; only a completed replacement can erase that evidence.
    """
    candidates = {}
    python_checks = python_checks or python_source_checks()
    for node in histories:
        origin = id(node["records"][0]) if node["records"] else None
        records, positions = (serial_ownership_records(node["records"]) if node["harness"] == "codex"
                              else (node["records"], list(range(len(node["records"])))))
        launches = {identifier: positions[index] for index, record in enumerate(records)
                    for identifier, _, _ in calls_and_results(record, node["harness"])[0]}
        writes = []
        for identifier, name, value, cwd, stamp in native_calls(records, node["harness"], with_timestamp=True):
            for effect in native_file_effects(name, value):
                if effect["kind"] == "write":
                    writes.append((identifier, name, cwd, native_timestamp(stamp), effect))
        selected = {identifier for identifier, _, _, _, _ in writes}
        ends, successful = native_write_outcomes(records, node["harness"], selected) if selected else ({}, set())
        by_call, sources, local_candidates = {}, {}, set()
        for identifier, name, cwd, timestamp, effect in writes:
            path = native_path(effect["path"], native_cwd(effect.get("cwd"), cwd)) if effect["path"] else None
            if path:
                entry = by_call.setdefault((identifier, path), {"at": timestamp, "owns": False, "final": None, "context": []})
                owning = apply_file_effect(effect, cwd, local_candidates, sources, python_checks=python_checks)
                entry["context"].append(dict(effect, path=path, cwd=native_cwd(effect.get("cwd"), cwd),
                                             _native_update=name if name in {"apply_patch", "functions.apply_patch", "Edit"} and "updates" in effect else None))
                entry["owns"] |= owning
                if owning or effect["replaces"]:
                    entry["final"] = owning
        completions = {identifier: native_timestamp(records[index].get("timestamp"))
                       for identifier, index in ends.items()}
        for (identifier, path), entry in by_call.items():
            completion = completions.get(identifier) if entry["at"] is not None else None
            if completion is None and entry["at"] is not None:
                completion = float("inf")
            context = tuple(entry["context"])
            completed_context = tuple(dict(effect, _native_success=identifier in successful) for effect in context)
            if entry["owns"] or entry["context"]:
                candidates.setdefault(path, []).append((origin, entry["at"], entry["owns"], False, completion, identifier, context,
                                                       entry["at"], launches.get(identifier), False))
                if completed_context and completion is not None and completion != float("inf"):
                    candidates[path].append((origin, completion, entry["final"] is True, any(effect["replaces"] for effect in completed_context),
                                             completion, identifier, completed_context,
                                             entry["at"], launches.get(identifier), True))
            if entry["final"] is False and (completion is None or completion == float("inf")):
                candidates.setdefault(path, []).append((origin, completion, False, True, completion, identifier, completed_context,
                                                       entry["at"], launches.get(identifier), True))
    return candidates


def execution_targets(argv):
    child = unwrapped_command(argv)
    if not child:
        return []
    name = Path(child[0]).name
    if name == "emulate":
        return [path for body in executor_commands(child)
                for command in shell_commands("exec_command", {"cmd": body}) for path in execution_targets(command)]
    if name == "hash":
        return []  # Installing or querying a binding does not run its target.
    interprets = child[0] == "." or name in {"bash", "sh", "zsh", "dash", "ksh", "source"} or re.fullmatch(r"python[0-9.]*", name)
    if interprets:
        if any(option == "-m" or option.startswith("-") and "c" in option for option in child[1:]):
            return []
        if "<" in child:
            return child[child.index("<") + 1:child.index("<") + 2]
        return next(([word] for word in child[1:] if not word.startswith("-")), [])
    return [child[0]]


def file_ownership_executions(records, harness, *, file_candidates=(), python_checks=None):
    """Link actual script reads to the contents preceding them in native time."""
    python_checks = python_checks or python_source_checks()
    origin = id(records[0]) if records else None
    calls = [(identifier, name, value, cwd, stamp, list(native_file_effects(name, value)))
             for identifier, name, value, cwd, stamp in native_calls(records, harness, with_timestamp=True)]
    writers = {identifier for identifier, _, _, _, _, effects in calls
               if any(effect["kind"] == "write" for effect in effects)}
    launches = {identifier: index for index, record in enumerate(records)
                for identifier, _, _ in calls_and_results(record, harness)[0]}
    ends, successful = None, set()

    def completion_ends():
        nonlocal ends, successful
        if ends is None:
            ends, successful = native_write_outcomes(records, harness, writers)
        return ends

    own_intervals, completed = {}, []
    candidates, peer_writes, intervals, sources, contributions = set(), [], [], {}, {}
    generation, active_projection, settled = 0, None, {}
    if isinstance(file_candidates, dict):
        for path, events in file_candidates.items():
            for event in events:
                source, timestamp, owning, replaces, completion, identifier = event[:6]
                context = event[6] if len(event) > 6 else ()
                started, index, completed_write = event[7:] if len(event) > 7 else (timestamp, None, replaces)
                if source != origin and timestamp != float("inf"):
                    peer_writes.append((timestamp, path, owning, replaces, source, context,
                                        (source, identifier, started, index, completion), completed_write))
                    if not completed_write and timestamp is not None and owning:
                        intervals.append((path, timestamp, completion, context))
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

    def newer_than(left, right, *, overlap=False):
        if left[:2] == right[:2]:
            return False
        if left[0] == right[0] and left[3] is not None and right[3] is not None:
            return left[3] > right[3] or overlap and len(left) > 5 and left[5] >= right[3]
        # A peer that was in flight when replacement launched may have
        # written after it, even if its terminal result arrived first.
        completed_at = left[4] if len(left) > 4 else None
        return (left[2] is None or right[2] is None or left[2] >= right[2]
                or overlap and (completed_at is None or completed_at >= right[2]))

    def remember(path, writer, context, *, append=False):
        entries = contributions.setdefault(path, {})
        previous = entries.get(writer[:2], (writer, ()))
        entries[writer[:2]] = (writer, (*previous[1], *context) if append else tuple(context))

    def source_ready(path, current=None):
        # A copy can observe the bytes present at any point in its interval.
        # A settled source first written after its launch is not that proof.
        return all(current is not None and key == current[:2]
                   or settled.get(key, False) and (current is None or not newer_than(entry[0], current, overlap=True))
                   for key, entry in contributions.get(path, {}).items())

    def apply_context(context, target_candidates, target_sources, *, possible=False, current=None):
        for effect in context:
            if possible and effect.get("_native_update"):
                apply_file_effect(effect, None, target_candidates, dict(target_sources), python_checks=python_checks, source_ready=lambda path: source_ready(path, current))
                continue
            if possible and effect["replaces"]:
                effect = dict(effect, replaces=False, partial=False)
            elif possible and "updates" in effect:
                effect = dict(effect, partial=True)
            apply_file_effect(effect, None, target_candidates, target_sources, python_checks=python_checks, source_ready=lambda path: source_ready(path, current))

    def replay(events, target_candidates, target_sources):
        nonlocal generation
        if events:
            generation += 1
        final = {}
        for path, owning, replaces, source, context, writer, finished in events:
            if finished and not all(effect.get("_native_success") is True for effect in context):
                replaces = False
                context = tuple(dict(effect, replaces=False, partial=False) if effect["replaces"] else effect for effect in context)
            newer = [entry for entry in contributions.get(path, {}).values()
                     if finished and replaces and newer_than(entry[0], writer)]
            snapshot = None
            native_update = next((effect.get("_native_update") for effect in context if effect.get("_native_update")), None)
            if finished and native_update:
                safe_base = all(settled.get(key, False) for key in contributions.get(path, {}) if key != writer[:2])
                projected = dict(target_sources)
                projected_candidates = set(target_candidates)
                apply_context(context, projected_candidates, projected, current=writer)
                snapshot = (completed_python_update(native_update, path, context, projected.get(path, {}), python_checks=python_checks)
                            if safe_base and all(effect.get("_native_success") is True for effect in context) else None)
                if snapshot is not None:
                    context, replaces = snapshot, True
                else:
                    # Failed/unknown writes remain possible; their text cannot
                    # certify the base for another native erasure.
                    context = tuple(dict(effect, partial=True) if effect.get("_native_success") is not True else effect for effect in context)
                newer = [entry for entry in contributions.get(path, {}).values() if replaces and newer_than(entry[0], writer, overlap=True)]
            if context:
                # A partial write's bytes were already applied at launch.
                # Replay them only when an intervening replacement removed
                # its contribution while the native writer was still alive.
                if not (finished and not replaces and not native_update and writer[:2] in contributions.get(path, {})):
                    apply_context(context, target_candidates, target_sources, possible=not finished, current=writer)
                owning = path in target_candidates
            if finished:
                settled[writer[:2]] = snapshot is not None or all(effect.get("_native_success") is True for effect in context)
                if not settled[writer[:2]] and path in target_sources:
                    target_sources[path] = dict(target_sources[path], erasure_exact=False)
                remember(path, writer, context)
                if replaces:
                    contributions[path] = {key: entry for key, entry in contributions[path].items()
                                           if key == writer[:2] or newer_than(entry[0], writer, overlap=bool(native_update))}
                # Replay only later writes' own effects. Their cumulative
                # pre-replacement state can contain bytes this writer erased.
                for later_writer, later in newer:
                    apply_context(later, target_candidates, target_sources, possible=True, current=later_writer)
                owning = path in target_candidates if context or newer else owning
            else:
                remember(path, writer, context)
            if owning or replaces:
                final.setdefault(path, {})[source] = owning
        for path, origins in final.items():
            if any(origins.values()):
                target_candidates.add(path)
            else:
                target_candidates.discard(path)

    next_peer = 0
    invocations = set()
    for identifier, name, value, cwd, stamp, effects in calls:
        timestamp = native_timestamp(stamp)
        read_at = launches.get(identifier)
        # Only already-launched writes can contribute their completed state.
        # Merge those native completions with peers in timestamp order; an own
        # completion with no timestamp still has exact native record order.
        due = [(at, event) for index, at, event in sorted(completed, key=lambda entry: entry[0])
               if read_at is not None and index < read_at]
        completed = [(index, at, event) for index, at, event in completed if read_at is None or index >= read_at]
        if due:
            upcoming = {}
            for at, events in groups[next_peer:]:
                upcoming.setdefault(at, []).extend(events)
            for at, event in due:
                upcoming.setdefault(at, []).append(event)
            groups[next_peer:] = sorted(upcoming.items(), key=lambda item: float("-inf") if item[0] is None else item[0])
        while next_peer < len(groups):
            at, events = groups[next_peer]
            if at is not None and timestamp is not None and at > timestamp:
                break
            if at is not None and at == timestamp:
                # Own native completion order is known even when timestamps
                # tie. Foreign same-time events remain possible alternatives.
                local = [event for event in events if event[3] == origin]
                replay(local, candidates, sources)
                events = [event for event in events if event[3] != origin]
                groups[next_peer] = at, events
                if not events:
                    next_peer += 1
                    continue
                speculative = dict(sources)
                speculative_candidates = set(candidates)
                previous_candidates = set(candidates)
                for path, owning, _, _, context, writer, _ in events:
                    remember(path, writer, context)
                    apply_context(context, speculative_candidates, speculative, possible=True, current=writer)
                    owning |= path in speculative_candidates
                    if owning:
                        candidates.add(path)
                if candidates != previous_candidates:
                    generation += 1
                break
            replay(events, candidates, sources)
            next_peer += 1
        # The current invocation observes its own ordered writes immediately.
        # Other invocations retain old and possible new bytes until a bound
        # terminal result proves a full replacement has finished.
        local_candidates, local_sources, replacements, written_by_path = set(candidates), dict(sources), {}, {}
        entry_candidates, entry_sources, preceding_writes = set(candidates), dict(sources), []
        for effect in effects:
            if effect["kind"] == "write":
                if name in {"apply_patch", "functions.apply_patch", "Edit"} and "updates" in effect:
                    effect = dict(effect, _native_update=name)
                generation += 1
                path = native_path(effect["path"], native_cwd(effect.get("cwd"), cwd)) if effect["path"] else None
                preceding_writes.append(effect)
                local_effect = dict(effect, replaces=False) if effect.get("_replace_success") else effect
                owning = apply_file_effect(local_effect, cwd, local_candidates, local_sources, python_checks=python_checks,
                                           source_ready=lambda path: source_ready(path, (origin, identifier, timestamp, read_at)))
                possible = (dict(effect, replaces=False, partial=False) if effect["replaces"] else
                            dict(effect, partial=True) if "updates" in effect else effect)
                apply_file_effect(possible, cwd, candidates, dict(sources) if effect.get("_native_update") else sources,
                                  python_checks=python_checks, source_ready=lambda path: source_ready(path, (origin, identifier, timestamp, read_at)))
                if path:
                    if effect["replaces"]:
                        replacements[path] = []
                    if path in replacements:
                        replacements[path].append(dict(effect, path=path, cwd=native_cwd(effect.get("cwd"), cwd)))
                    written = written_by_path.setdefault(path, [])
                    written.append(dict(effect, path=path, cwd=native_cwd(effect.get("cwd"), cwd)))
                    if owning:
                        own_intervals[(path, identifier)] = read_at, written
                    writer = (origin, identifier, timestamp, read_at)
                    remember(path, writer, [dict(effect, path=path, cwd=native_cwd(effect.get("cwd"), cwd))], append=True)
            else:
                observed_candidates, observed_sources = local_candidates, local_sources
                required = effect.get("_implied_success", ())
                proven_writes = frozenset(written.get("position") for written in preceding_writes
                                         if written.get("_replace_success") and written.get("position") in required)
                if proven_writes:
                    observed_candidates, observed_sources = set(entry_candidates), dict(entry_sources)
                    for written in preceding_writes:
                        if "_replace_success" in written:
                            written = dict(written, replaces=bool(written["_replace_success"] and written.get("position") in required))
                        apply_file_effect(written, cwd, observed_candidates, observed_sources, python_checks=python_checks,
                                          source_ready=lambda path: source_ready(path, (origin, identifier, timestamp, read_at)))
                active_contexts = [context for _, start, end, context in intervals
                                   if timestamp is None or start <= timestamp <= end]
                # Later calls can run while an earlier writer's handle lives.
                # Record order excludes future writes without timestamps; the
                # current invocation keeps its explicit shell/patch ordering.
                if own_intervals:
                    finished = completion_ends()
                    active_contexts.extend(context for (_, source), (began, context) in own_intervals.items()
                                           if source != identifier and began is not None and read_at is not None
                                           and began < read_at <= finished.get(source, float("inf")))
                key = generation, tuple(id(context) for context in active_contexts), proven_writes
                # Pure reads do not change a writer's possible bytes. Reuse
                # their projection until a write, completion, or interval
                # membership change affects the source/candidate state.
                if active_projection is None or active_projection[0] != key:
                    active = set()
                    for context in active_contexts:
                        speculative_candidates, speculative_sources = set(observed_candidates), dict(observed_sources)
                        apply_context(context, speculative_candidates, speculative_sources, possible=True)
                        active.update(speculative_candidates)
                    active_projection = key, active
                else:
                    active = active_projection[1]
                if {native_path(path, native_cwd(effect.get("cwd"), cwd)) for path in effect["paths"]} & (observed_candidates | active):
                    invocations.add(identifier)
        for path, written in written_by_path.items():
            context = replacements.get(path, written)
            end = completion_ends().get(identifier)
            if end is not None:
                context = tuple(dict(effect, _native_success=identifier in successful) for effect in context)
                at = native_timestamp(records[end].get("timestamp"))
                event = (path, path in local_candidates, path in replacements, origin, tuple(context),
                         (origin, identifier, timestamp, read_at, at, end), True)
                completed.append((end, at, event))
    return invocations


class GroupShellLexer(shlex.shlex):
    """Preserve raw shell words and split adjacent structural punctuation."""

    def __init__(self, *args, **kwargs):
        self._raw_source = kwargs.pop("raw_source", None)
        self._protected_words = kwargs.pop("protected_words", {})
        super().__init__(*args, **kwargs)
        self._word_cursor = 0
        self.wordchars += "".join({word[0] for word in self._protected_words})

    @staticmethod
    def words(text):
        lexer = shell_lexer(text, punctuation_chars="")
        lexer.whitespace_split = True
        return list(lexer)

    @staticmethod
    def protected_word(text, start):
        """Keep shell quoting/expansion operators inside their own word."""
        if text.startswith("$'", start):
            close = start + 2
            while close < len(text):
                if text[close] == "'":
                    break
                close += 2 if text[close] == "\\" else 1
            if close >= len(text):
                raise ValueError("unclosed ANSI-C quote")
            # Raw escaped pairs determine the quote boundary before a control
            # escape can consume a backslash that protected an apostrophe.
            body = text[start + 2:close]
            end, pieces = 0, []
            while end < len(body):
                char = body[end]
                if char == "\\" and end + 1 < len(body):
                    following = body[end + 1]
                    if following in "'\"?":
                        piece, end = following, end + 2
                    elif following == "c" and end + 2 < len(body):
                        control = body[end + 2]
                        piece, end = chr(127 if control == "?" else ord(control) & 31), end + 3
                    else:
                        initial = end
                        piece, end, _ = producer_escape(body, end, mode="format")
                        if following in "01234567" and len(piece) == 1:
                            piece = chr(ord(piece) & 255)
                        if end <= initial:
                            raise ValueError("unparsed ANSI-C escape")
                    pieces.append(piece)
                    continue
                pieces.append(char)
                end += 1
            return close + 1, "".join(pieces).split("\0", 1)[0]
        bracket = text.startswith("$[", start)
        arithmetic = 3 if text.startswith("$((", start) else 2 if bracket or text.startswith("((", start) else 0
        if not arithmetic:
            return None
        end, stack, quote, escaped = start + arithmetic, ["]" if bracket else ")"], None, False
        while end < len(text):
            char = text[end]
            if escaped:
                escaped = False
            elif char == "\\" and quote != "'":
                escaped = True
            elif quote:
                if char == quote:
                    quote = None
            elif text.startswith("$'", end):
                end, _ = GroupShellLexer.protected_word(text, end)
                continue
            elif char in "'\"`":
                quote = char
            elif char == "(" or bracket and char == "[":
                stack.append(")" if char == "(" else "]")
            elif char == stack[-1]:
                stack.pop()
                if not stack:
                    if not bracket and text[end + 1:end + 2] != ")":
                        return None  # Nested subshell, not arithmetic.
                    end += 1 if bracket else 2
                    return end, text[start:end]
            end += 1
        raise ValueError("unclosed arithmetic expansion")

    def read_token(self):
        if self.posix:
            token = super().read_token()
            if isinstance(token, str):
                for word, decoded in self._protected_words.items():
                    token = token.replace(word, decoded)
            return token
        # Let shlex's POSIX quote state handle quotes inside an option word,
        # while retaining the raw source slice needed for descriptor adjacency.
        source = self._raw_source if self._raw_source is not None else self.instream.getvalue()
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
        source = self._raw_source if self._raw_source is not None else self.instream.getvalue()
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
    cleaned, masked, protected = [], [], {}
    quote, escaped, boundary, comment, cursor, marker = None, False, True, False, 0, 0xe000
    occupied = set(text)
    while cursor < len(text):
        char = text[cursor]
        if comment:
            if char != "\n":
                cursor += 1
                continue
            comment = False
        if escaped:
            escaped = False
            if char == "\n":
                cleaned.pop()  # Shell continuation is removed before tokenization.
                masked.pop()
                cursor += 1
                continue
            boundary = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
            elif (quote == '"' and text.startswith(("$((", "$["), cursor)
                  and (word_result := GroupShellLexer.protected_word(text, cursor)) is not None):
                end, decoded = word_result
                while chr(marker) in occupied:
                    marker += 1
                word = chr(marker) * (end - cursor)
                occupied.add(chr(marker))
                protected[word] = decoded
                cleaned.append(text[cursor:end])
                masked.append(word)
                cursor = end
                continue
        elif (text.startswith(("$'", "$((", "((", "$["), cursor)
              and (word_result := GroupShellLexer.protected_word(text, cursor)) is not None):
            end, decoded = word_result
            while chr(marker) in occupied:
                marker += 1
            word = chr(marker) * (end - cursor)
            occupied.add(chr(marker))
            protected[word] = decoded
            cleaned.append(text[cursor:end])
            masked.append(word)
            boundary, cursor = False, end
            continue
        elif char in {"'", '"'}:
            quote, boundary = char, False
        elif char == "#" and boundary:
            comment = True
            cursor += 1
            continue
        else:
            boundary = char.isspace() or char in ";&|<>()"
        cleaned.append(char)
        masked.append(char)
        cursor += 1
    lexer = GroupShellLexer("".join(masked), posix=posix, punctuation_chars=punctuation_chars,
                            raw_source="".join(cleaned), protected_words=protected)
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


def ownership_state(records, harness, *, file_candidates=(), python_checks=None):
    """Track launches through their terminal native result, including process polls."""
    positions = list(range(len(records)))
    if harness == "codex":
        records, positions = serial_ownership_records(records)
    no_writes = no_write_claims(records, harness)
    def owning(name, value):
        return opaque_ownership(name, value) or any(ownership_command(argv) for argv in shell_commands(name, value))
    indices, pending, events = [], set(), {}
    for index, source, value in bound_outputs(records, harness, owning, isolated_polls=True,
                                             opaque_calls=file_ownership_executions(records, harness, file_candidates=file_candidates, python_checks=python_checks)):
        if source in no_writes:
            continue
        events.setdefault(index, []).append((source, value))
    tasks, running, scheduled, task_tools = {}, {}, {}, {}
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
            session_record.track_claude_background_tasks(record, running, scheduled, task_tools)
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
                        if ("&" in list(lexer) or any(unwrapped_command(argv, stop_at={"coproc"})[:1] == ["coproc"]
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
    python_checks = python_source_checks()
    parsed = list(outputs(records, harness, python_checks=python_checks))
    histories = histories or [{"session_id": session, "harness": harness, "records": records}]
    sessions = {h["session_id"] for h in histories}
    expected, errors = [], []
    root_expected = []
    child_activity, pending, root_state = False, False, None
    file_candidates = ownership_file_candidates(histories, python_checks=python_checks)
    for node in histories:
        node_parsed = parsed if node["records"] is records and node["harness"] == harness else list(outputs(node["records"], node["harness"], python_checks=python_checks))
        for native in sessions:
            found, missing = sources(node["records"], node["harness"], native, parsed=node_parsed)
            expected.extend(found)
            errors.extend(missing)
            if node is histories[0]:
                root_expected.extend(found)
        state = ownership_state(node["records"], node["harness"], file_candidates=file_candidates, python_checks=python_checks)
        if node["records"] is records and node["harness"] == harness:
            root_state = state
        pending = pending or state["pending"]
        if node is not histories[0] and state["indices"]:
            child_activity = True
    if pending:
        return {"clear": False, "reason": "ownership_activity_incomplete"}
    activity = (root_state if root_state is not None else ownership_state(records, harness, python_checks=python_checks))["indices"]
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
