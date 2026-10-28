# /// script
# requires-python = ">=3.12"
# dependencies = ["tree-sitter==0.26.0", "tree-sitter-javascript==0.25.0"]
# ///
from __future__ import annotations

import ast
import re

from tree_sitter import Language, Node, Parser

LITERAL_SHELL_VALUE = re.compile(r"[A-Za-z0-9_./@:+,%-][A-Za-z0-9_./@:+,%#-]*")
LITERAL_EXPANSION_BYTES = 1 << 20
LITERAL_LOOP_VALUES = 64

NATIVE_NAMES = {"tools", "text", "store", "load", "exit", "Promise", "JSON", "Object", "String", "ALL_TOOLS", "undefined"}
SHELL_TOOLS = {"exec_command", "shell_command", "shell", "write_stdin"}
GITHUB_READ_TOOL = re.compile(r"mcp__codex_apps__github_(?:fetch|get|list|search|compare|download)(?:_[a-z0-9_]+)?")
PROTOTYPE_NAMES = {
    "constructor",
    "prototype",
    "__proto__",
    "__defineGetter__",
    "__defineSetter__",
    "__lookupGetter__",
    "__lookupSetter__",
}
DATA_METHODS = {
    "includes",
    "indexOf",
    "join",
    "slice",
    "concat",
    "at",
    "trim",
    "split",
    "startsWith",
    "endsWith",
    "toLowerCase",
    "toUpperCase",
}
CALLBACK_METHODS = {"map", "filter", "flatMap", "forEach", "find", "findIndex", "some", "every"}


def children(node: Node | None) -> list[Node]:
    if node is None:
        raise ValueError("missing node")
    return [child for child in node.named_children if child.type != "comment"]


def array_elements(node: Node) -> list[Node]:
    elements = []
    expecting_value = True
    for child in node.children[1:-1]:
        if child.type == "comment":
            continue
        if child.type == ",":
            if expecting_value:
                raise ValueError("array hole")
            expecting_value = True
        else:
            if not expecting_value:
                raise ValueError("array")
            elements.append(child)
            expecting_value = False
    return elements


def source(node: Node | None) -> str:
    if node is None:
        raise ValueError("missing node")
    return node.text.decode()


def literal_string(node: Node) -> str:
    value = source(node)
    if re.fullmatch(r"(?:[^\\]|\\(?:[\\\"'nrtbfv]|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}))*", value) is None:
        raise ValueError("string")
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError) as error:
        raise ValueError("string") from error


def property_name(node: Node) -> str:
    value = source(node)
    if node.type == "string":
        value = literal_string(node)
    elif (
        node.type not in {"identifier", "property_identifier", "shorthand_property_identifier"}
        or re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value) is None
    ):
        raise ValueError(value)
    if value in PROTOTYPE_NAMES:
        raise ValueError(value)
    return value


def refusal(node: Node, reason: str, *, offset: int | None = None) -> dict:
    root = node
    while root.parent is not None:
        root = root.parent
    prefix = root.text[:node.start_byte if offset is None else offset]
    call = node
    while call.parent is not None and call.type != "call_expression":
        call = call.parent
    callee = call.child_by_field_name("function") if call.type == "call_expression" else node
    return {
        "line": prefix.count(b"\n") + 1,
        "column": len(prefix.rsplit(b"\n", 1)[-1].decode()) + 1,
        "syntax": node.type,
        "call": source(callee)[:120],
        "reason": reason,
    }


def native_property_condition(node: Node) -> bool:
    parent = node.parent
    while parent is not None and parent.type in {"await_expression", "parenthesized_expression"}:
        node = parent
        parent = node.parent
    if parent is None:
        return False
    if parent.type == "unary_expression" and source(parent.child_by_field_name("operator")) in {"!", "typeof"}:
        return True
    return parent.type in {
        "ternary_expression",
        "if_statement",
        "while_statement",
    } and node == parent.child_by_field_name("condition")


class JavascriptData:
    def __init__(self) -> None:
        self.environment: dict[str, tuple[str, bool]] = {"ALL_TOOLS": ("data", False), "undefined": ("data", False)}

    def bind(self, node: Node | None, environment: dict, *, kind: str = "data", mutable: bool = False) -> None:
        if node is None:
            raise ValueError("missing binding")
        if node.type == "identifier":
            name = property_name(node)
            if name in NATIVE_NAMES:
                raise ValueError(name)
            environment[name] = (kind, mutable)
        elif node.type in {"array_pattern", "formal_parameters"}:
            for child in array_elements(node) if node.type == "array_pattern" else children(node):
                self.bind(child, environment)
        else:
            raise ValueError(node.type)

    def expression(self, node: Node | None, environment: dict) -> str:
        if node is None:
            raise ValueError("missing expression")
        try:
            return self._expression(node, environment)
        except (KeyError, ValueError) as error:
            error.__dict__.setdefault("node", node)
            raise

    def _expression(self, node: Node, environment: dict) -> str:
        kind = node.type
        if kind == "regex":
            call = node.parent.parent if node.parent.type == "arguments" else None
            callee = call.child_by_field_name("function") if call is not None else None
            if (
                callee is None
                or callee.type != "member_expression"
                or property_name(callee.child_by_field_name("property")) not in {"split", "replace", "replaceAll"}
            ):
                raise ValueError("regex value")
            return "data"
        if kind == "arrow_function":
            local = environment.copy()
            parameters = node.child_by_field_name("parameter") or node.child_by_field_name("parameters")
            if parameters is not None:
                self.bind(parameters, local)
            body = node.child_by_field_name("body")
            if body.type == "statement_block":
                self.statement(body, local)
            else:
                self.expression(body, local)
            return "function"
        if kind == "assignment_expression":
            left = node.child_by_field_name("left")
            if left.type != "identifier" or environment.get(source(left)) != ("data", True):
                raise ValueError("assignment")
            result = self.expression(node.child_by_field_name("right"), environment)
            if result != "data":
                raise ValueError("assignment")
            return result
        if kind in {"identifier", "shorthand_property_identifier"}:
            return environment[source(node)][0]
        if kind == "number":
            return "number"
        if kind in {"string", "true", "false", "null", "undefined"}:
            return "data"
        if kind in {"member_expression", "subscript_expression"}:
            receiver = node.child_by_field_name("object")
            member = node.child_by_field_name("property" if kind == "member_expression" else "index")
            if receiver.type == "identifier" and source(receiver) == "tools" and kind == "member_expression":
                property_name(member)
                if not native_property_condition(node):
                    raise ValueError("native capability value")
                return "capability"
            receiver_kind = self.expression(receiver, environment)
            if kind == "member_expression" or member.type == "string":
                name = property_name(member)
                if receiver_kind == "array" and name == "length":
                    return "number"
            elif self.expression(member, environment) != "number":
                raise ValueError(kind)
            return "data"
        if kind == "call_expression":
            return self.call(node, environment)
        if kind == "update_expression":
            argument = node.child_by_field_name("argument")
            if argument.type != "identifier" or environment.get(source(argument)) != ("number", True):
                raise ValueError(kind)
            return "number"
        if kind == "object":
            for child in children(node):
                if child.type == "pair":
                    property_name(child.child_by_field_name("key"))
                    self.expression(child.child_by_field_name("value"), environment)
                else:
                    self.expression(child, environment)
            return "data"
        if kind == "unary_expression" and source(node.child_by_field_name("operator")) == "delete":
            raise ValueError(kind)
        if kind in {
            "array",
            "parenthesized_expression",
            "await_expression",
            "spread_element",
            "template_string",
            "template_substitution",
            "binary_expression",
            "unary_expression",
            "ternary_expression",
        }:
            if kind == "binary_expression" and source(node.child_by_field_name("operator")) == "/":
                raise ValueError(kind)
            results = [
                self.expression(child, environment)
                for child in children(node)
                if child.type not in {"string_fragment", "escape_sequence", "comment"}
            ]
            if kind == "array" or (kind in {"await_expression", "parenthesized_expression"} and results == ["array"]):
                return "array"
            if results == ["number"] and (
                kind == "parenthesized_expression"
                or (kind == "unary_expression" and source(node.child_by_field_name("operator")) in {"+", "-", "~"})
            ):
                return "number"
            return "data"
        raise ValueError(kind)

    def call(self, node: Node, environment: dict) -> str:
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if (
            callee is None
            or arguments is None
            or arguments.type != "arguments"
            or any(child.type == "optional_chain" for child in node.children)
        ):
            raise ValueError("call")
        args = children(arguments)
        result_kind = "data"
        if any(argument.type == "spread_element" for argument in args):
            raise ValueError("spread arguments")
        if callee.type == "parenthesized_expression" and len(children(callee)) == 1:
            function = children(callee)[0]
            parameters = function.child_by_field_name("parameters")
            if function.type not in {"arrow_function", "function_expression"} or parameters is None or children(parameters) or args:
                raise ValueError("immediate callback")
            self.statement(function.child_by_field_name("body"), environment.copy())
            return "promise" if any(child.type == "async" for child in function.children) else "data"
        if callee.type == "identifier" and (
            source(callee) in {"text", "image", "store", "load", "exit"} and source(callee) not in environment
            or environment.get(source(callee), (None,))[0] == "function"
            or source(callee) == "String"
            and len(args) <= 1
        ):
            pass
        elif callee.type == "member_expression":
            receiver = callee.child_by_field_name("object")
            method = property_name(callee.child_by_field_name("property"))
            native = source(receiver) if receiver.type == "identifier" else ""
            if native == "tools" and (
                method
                in SHELL_TOOLS
                | {"apply_patch", "clock__curr_time", "web__run", "view_image", "codex_tui__list_threads", "codex_tui__read_thread"}
                or GITHUB_READ_TOOL.fullmatch(method)
            ):
                result_kind = "promise"
            elif native == "Promise" and method in {"all", "allSettled"} and len(args) == 1:
                result_kind = "array"
            elif native == "JSON" and method in {"parse", "stringify"} and len(args) == 1:
                pass
            elif native == "Object" and method in {"keys", "values", "entries"} and len(args) == 1:
                result_kind = "array"
            elif receiver.type == "regex":
                if (
                    method != "test"
                    or source(callee) != source(receiver) + ".test"
                    or len(args) != 1
                    or source(receiver).rpartition("/")[2] not in {"", "i"}
                ):
                    raise ValueError("regex predicate")
            else:
                receiver_kind = self.expression(receiver, environment)
                if (method in CALLBACK_METHODS or method == "then" and receiver_kind == "promise") and len(args) == 1:
                    callback = args[0]
                    if method == "forEach" and callback.type == "identifier" and source(callback) == "text":
                        return "data"
                    if callback.type != "arrow_function":
                        raise ValueError("callback")
                    local = environment.copy()
                    parameters = callback.child_by_field_name("parameter") or callback.child_by_field_name("parameters")
                    if parameters is not None:
                        patterns = children(parameters) if parameters.type == "formal_parameters" else [parameters]
                        for index, parameter in enumerate(patterns):
                            kind = "number" if receiver_kind == "array" and index == 1 else "data"
                            self.bind(parameter, local, kind=kind)
                    body = callback.child_by_field_name("body")
                    if body.type == "statement_block":
                        self.statement(body, local)
                    else:
                        self.expression(body, local)
                    return "array" if receiver_kind == "array" and method in {"map", "filter", "flatMap"} else "data"
                if receiver_kind == "array" and method in {"slice", "concat"}:
                    result_kind = "array"
                literal_replacement = (
                    method in {"replace", "replaceAll"}
                    and len(args) == 2
                    and args[1].type == "string"
                    and (args[0].type == "string" or args[0].type == "regex")
                )
                if method not in DATA_METHODS and not literal_replacement:
                    raise ValueError(method)
        else:
            raise ValueError("callee")
        for argument in args:
            self.expression(argument, environment)
        return result_kind

    def statement(self, node: Node, environment: dict) -> None:
        try:
            self._statement(node, environment)
        except (KeyError, ValueError) as error:
            error.__dict__.setdefault("node", node)
            raise

    def _statement(self, node: Node, environment: dict) -> None:
        kind = node.type
        if kind in {"program", "statement_block"}:
            local = environment.copy() if kind == "statement_block" else environment
            for child in children(node):
                self.statement(child, local)
        elif kind in {"comment", "hash_bang_line", "empty_statement", "break_statement", "continue_statement"}:
            return
        elif kind == "lexical_declaration":
            mutable = node.children[0].type == "let"
            for declaration in children(node):
                value = declaration.child_by_field_name("value")
                value_kind = self.expression(value, environment) if value is not None else "data"
                self.bind(declaration.child_by_field_name("name"), environment, kind=value_kind, mutable=mutable)
        elif kind == "for_in_statement":
            local = environment.copy()
            left = node.child_by_field_name("left")
            if node.child_by_field_name("kind") is None or source(node.child_by_field_name("kind")) not in {"const", "let"}:
                raise ValueError(kind)
            self.expression(node.child_by_field_name("right"), environment)
            self.bind(left, local)
            self.statement(node.child_by_field_name("body"), local)
        elif kind in {
            "for_statement",
            "if_statement",
            "else_clause",
            "while_statement",
            "try_statement",
            "catch_clause",
            "finally_clause",
        }:
            local = environment.copy()
            parameter = node.child_by_field_name("parameter") if kind == "catch_clause" else None
            if parameter is not None:
                self.bind(parameter, local)
            for child in children(node):
                if child != parameter:
                    self.statement(child, local)
        elif kind in {"expression_statement", "return_statement", "throw_statement"}:
            for child in children(node):
                self.expression(child, environment)
        else:
            self.expression(node, environment)


class LiteralData:
    def __init__(self, shell_word: re.Pattern, count_limit: int, byte_limit: int) -> None:
        self.shell_word = shell_word
        self.count_limit = count_limit
        self.byte_limit = byte_limit

    def value(self, node: Node | None, environment: dict) -> object:
        if node is None:
            raise ValueError("missing value")
        return self._value(node, environment)

    def _value(self, node: Node, environment: dict) -> object:
        if node.type in {"identifier", "shorthand_property_identifier"}:
            value = environment.get(property_name(node))
            if value is None:
                raise ValueError("unknown binding")
            return value
        if node.type == "string":
            return literal_string(node)
        if node.type == "number":
            value = ast.literal_eval(source(node))
            if type(value) is not int or abs(value) > (1 << 53) - 1:
                raise ValueError("number")
            return value
        if node.type == "unary_expression" and source(node.child_by_field_name("operator")) in {"+", "-"}:
            value = self.value(node.child_by_field_name("argument"), environment)
            if type(value) is not int:
                raise ValueError("number")
            return -value if source(node.child_by_field_name("operator")) == "-" else value
        if node.type == "binary_expression" and source(node.child_by_field_name("operator")) == "+":
            left = self.value(node.child_by_field_name("left"), environment)
            right = self.value(node.child_by_field_name("right"), environment)
            if type(left) is str and type(right) is str and len((left + right).encode()) <= self.byte_limit:
                return left + right
            if type(left) is not int or type(right) is not int or abs(left + right) > (1 << 53) - 1:
                raise ValueError("number")
            return left + right
        if node.type == "array":
            result = []
            for child in array_elements(node):
                if child.type == "spread_element":
                    value = self.value(children(child)[0], environment)
                    if type(value) is not list:
                        raise ValueError("array spread")
                    result.extend(value)
                else:
                    result.append(self.value(child, environment))
                if len(result) > self.count_limit:
                    raise ValueError("array limit")
            return result
        if node.type == "subscript_expression":
            receiver = self.value(node.child_by_field_name("object"), environment)
            index = self.value(node.child_by_field_name("index"), environment)
            if type(receiver) is list and type(index) is int and 0 <= index < len(receiver):
                return receiver[index]
            if type(receiver) is dict and type(index) is str and index in receiver:
                return receiver[index]
            raise ValueError("subscript")
        if node.type == "object":
            result = {}
            for member in children(node):
                if member.type != "pair":
                    raise ValueError("object")
                name = property_name(member.child_by_field_name("key"))
                if name in result or name.isdecimal():
                    raise ValueError("object key")
                result[name] = self.value(member.child_by_field_name("value"), environment)
            if len(result) > self.count_limit:
                raise ValueError("object limit")
            return result
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            if function.type != "member_expression":
                raise ValueError("call")
            receiver = function.child_by_field_name("object")
            method = property_name(function.child_by_field_name("property"))
            args = children(node.child_by_field_name("arguments"))
            if method == "map" and len(args) == 1 and args[0].type == "arrow_function":
                values = self.value(receiver, environment)
                if type(values) is not list or any(child.type == "async" for child in args[0].children):
                    raise ValueError("literal map")
                pattern = args[0].child_by_field_name("parameter") or args[0].child_by_field_name("parameters")
                result = []
                for index, value in enumerate(values):
                    local = environment.copy()
                    self.bind(pattern, [value, index, values] if pattern.type == "formal_parameters" else value, local)
                    mapped = self.value(args[0].child_by_field_name("body"), local)
                    if type(mapped) not in {str, int}:
                        raise ValueError("literal map result")
                    result.append(mapped)
                return result
            arguments = [self.value(child, environment) for child in args]
            if source(receiver) == "Object" and method == "entries" and len(arguments) == 1 and type(arguments[0]) is dict:
                return [[key, value] for key, value in arguments[0].items()]
            value = self.value(receiver, environment)
            if method == "split" and type(value) is str and len(arguments) == 1 and type(arguments[0]) is str and arguments[0]:
                result = value.split(arguments[0])
                if len(result) <= self.count_limit:
                    return result
            if method == "at" and type(value) is list and len(arguments) == 1 and type(arguments[0]) is int:
                if -len(value) <= arguments[0] < len(value):
                    return value[arguments[0]]
            if method in {"replace", "replaceAll"} and type(value) is str and len(arguments) == 2:
                if all(type(argument) is str for argument in arguments) and arguments[0] and "$" not in arguments[1]:
                    return value.replace(arguments[0], arguments[1], 1 if method == "replace" else -1)
            raise ValueError("call")
        if node.type == "template_string" and "\\" not in source(node):
            parts = []
            for child in children(node):
                if child.type == "string_fragment":
                    parts.append(source(child))
                elif child.type == "template_substitution" and len(children(child)) == 1:
                    value = self.value(children(child)[0], environment)
                    if type(value) not in {str, int} or any(
                        self.shell_word.fullmatch(word.replace("=", "")) is None for word in str(value).split(" ")
                    ):
                        raise ValueError("interpolation")
                    parts.append(str(value))
                else:
                    raise ValueError("template")
            return "".join(parts)
        raise ValueError(node.type)

    def bind(self, pattern: Node | None, value: object, environment: dict) -> None:
        if pattern is None:
            raise ValueError("missing binding")
        if "\\" in source(pattern):
            raise ValueError("escaped binding")
        if pattern.type in {"identifier", "shorthand_property_identifier_pattern"}:
            environment[source(pattern)] = value
        elif pattern.type in {"array_pattern", "formal_parameters"}:
            elements = array_elements(pattern) if pattern.type == "array_pattern" else children(pattern)
            for index, child in enumerate(elements):
                item = value[index] if isinstance(value, list) and index < len(value) else None
                self.bind(child, item, environment)
        else:
            raise ValueError("binding")

    def environments(self, node: Node, *, strict: bool = False) -> list[dict]:
        lineage = []
        while node is not None:
            lineage.append(node)
            node = node.parent
        lineage.reverse()
        rows = [{}]
        for index, node in enumerate(lineage[:-1]):
            following = lineage[index + 1]
            if node.type in {"program", "statement_block"}:
                statements = children(node)
                for statement in statements:
                    if statement.type == "lexical_declaration":
                        for declaration in children(statement):
                            for row in rows:
                                self.bind(declaration.child_by_field_name("name"), None, row)
                for statement in statements:
                    if statement == following:
                        break
                    if statement.type != "lexical_declaration" or statement.children[0].type != "const":
                        continue
                    for declaration in children(statement):
                        for row in rows:
                            try:
                                value = self.value(declaration.child_by_field_name("value"), row)
                            except (ValueError, SyntaxError):
                                value = None
                            self.bind(declaration.child_by_field_name("name"), value, row)
            if strict and node.type in {"for_statement", "while_statement", "do_statement"}:
                raise ValueError("execution count")
            if node.type == "for_statement":
                initializer = node.child_by_field_name("initializer")
                if initializer is not None and initializer.type in {"lexical_declaration", "variable_declaration"}:
                    for declaration in children(initializer):
                        for row in rows:
                            self.bind(declaration.child_by_field_name("name"), None, row)
            if node.type == "catch_clause":
                parameter = node.child_by_field_name("parameter")
                if parameter is not None:
                    for row in rows:
                        self.bind(parameter, None, row)
            if node.type in {"arrow_function", "function_expression"}:
                pattern = node.child_by_field_name("parameter") or node.child_by_field_name("parameters")
                if pattern is None:
                    continue
                if node.parent.type == "parenthesized_expression" and pattern.type == "formal_parameters" and not children(pattern):
                    call = node.parent.parent
                    if call.type == "call_expression" and not children(call.child_by_field_name("arguments")):
                        continue
                call = node.parent.parent if node.parent.type == "arguments" else None
                callee = call.child_by_field_name("function") if call is not None else None
                receiver = None
                if (
                    callee is not None
                    and callee.type == "member_expression"
                    and source(callee.child_by_field_name("property")) in CALLBACK_METHODS
                ):
                    receiver = callee.child_by_field_name("object")
                expanded = []
                for row in rows:
                    try:
                        values = self.value(receiver, row) if receiver is not None else None
                    except (ValueError, SyntaxError):
                        values = None
                    if not isinstance(values, list) or not values:
                        if values == []:
                            continue
                        if strict:
                            raise ValueError("execution count")
                        local = row.copy()
                        self.bind(pattern, None, local)
                        expanded.append(local)
                        continue
                    for item_index, value in enumerate(values):
                        local = row.copy()
                        arguments = [value, item_index, values] if pattern.type == "formal_parameters" else value
                        self.bind(pattern, arguments, local)
                        expanded.append(local)
                rows = expanded
            if node.type == "for_in_statement" and following == node.child_by_field_name("body"):
                expanded = []
                for row in rows:
                    try:
                        values = self.value(node.child_by_field_name("right"), row)
                    except (ValueError, SyntaxError):
                        values = None
                    if (
                        source(node.child_by_field_name("kind")) != "const"
                        or source(node.child_by_field_name("operator")) != "of"
                        or not isinstance(values, list)
                        or not values
                    ):
                        if values == []:
                            continue
                        if strict:
                            raise ValueError("execution count")
                        local = row.copy()
                        self.bind(node.child_by_field_name("left"), None, local)
                        expanded.append(local)
                        continue
                    for value in values:
                        local = row.copy()
                        self.bind(node.child_by_field_name("left"), value, local)
                        expanded.append(local)
                rows = expanded
            if len(rows) > self.count_limit:
                raise ValueError("expansion limit")
        return rows

    def commands(self, arguments: Node, *, occurrences: bool = False) -> list[str]:
        args = children(arguments)
        if arguments.type != "arguments" or len(args) != 1 or args[0].type != "object":
            raise ValueError("arguments")
        command = None
        for field in children(args[0]):
            if field.type == "spread_element":
                raise ValueError("spread")
            if field.type == "pair":
                key = property_name(field.child_by_field_name("key"))
                value = field.child_by_field_name("value")
            elif field.type == "shorthand_property_identifier":
                key = property_name(field)
                value = field
            else:
                continue
            if key not in {"cmd", "command", "chars"}:
                continue
            if command is not None:
                raise ValueError("duplicate command")
            command = value
        if command is None:
            raise ValueError("missing command")
        if occurrences:
            commands = [self.value(command, row) for row in self.environments(arguments, strict=True)]
        else:
            try:
                commands = [self.value(command, {})]
            except (ValueError, SyntaxError):
                commands = [self.value(command, row) for row in self.environments(arguments)]
        if not all(isinstance(item, str) for item in commands) or sum(len(item.encode()) for item in commands) > self.byte_limit:
            raise ValueError("commands")
        return commands


class LiteralCommands(LiteralData):
    def __init__(self, shell_word: re.Pattern, count_limit: int, byte_limit: int) -> None:
        super().__init__(shell_word, count_limit, byte_limit)
        self.data = LiteralData(shell_word, count_limit, byte_limit)
        self.stores: dict[Node, dict[str, list[Node]] | None] = {}
        self.snapshots: dict[tuple[Node, str], object] = {}

    def store_index(self, root: Node) -> dict[str, list[Node]]:
        if root not in self.stores:
            self.stores[root] = None
            index: dict[str, list[Node]] = {}
            pending = [root]
            count = 0
            while pending:
                candidate = pending.pop()
                pending.extend(reversed(children(candidate)))
                if candidate.type != "call_expression":
                    continue
                function = candidate.child_by_field_name("function")
                if function.type != "identifier" or source(function) != "store":
                    continue
                arguments = candidate.child_by_field_name("arguments")
                args = children(arguments)
                if len(args) != 2:
                    raise ValueError("store arguments")
                try:
                    keys = [self.data.value(args[0], {})]
                except (ValueError, SyntaxError):
                    keys = [self.data.value(args[0], row) for row in self.data.environments(arguments)]
                for key in keys:
                    count += 1
                    if type(key) is not str or count > self.count_limit:
                        raise ValueError("store key")
                    key.encode()
                    index.setdefault(key, []).append(arguments)
            self.stores[root] = index
        result = self.stores[root]
        if result is None:
            raise ValueError("unknown store keys")
        return result

    def stored_value(self, node: Node, key: str) -> object:
        root = node
        while root.parent is not None:
            root = root.parent
        writers = self.store_index(root).get(key, [])
        if len(writers) != 1:
            raise ValueError("store writer")
        arguments = writers[0]
        statement = arguments.parent
        ancestor = statement.parent
        loops = []
        while ancestor != root:
            if ancestor.type not in {"expression_statement", "statement_block", "for_in_statement"}:
                raise ValueError("conditional store")
            if ancestor.type == "for_in_statement":
                loops.append(ancestor)
            statement = ancestor
            ancestor = ancestor.parent
        if statement.end_byte >= node.start_byte:
            raise ValueError("store order")
        ancestor = node.parent
        while ancestor != root:
            if ancestor.type in {
                "arrow_function",
                "function_expression",
                "for_in_statement",
                "for_statement",
                "while_statement",
                "do_statement",
            }:
                raise ValueError("deferred load")
            ancestor = ancestor.parent
        pending = [root]
        while pending:
            candidate = pending.pop()
            pending.extend(reversed(children(candidate)))
            suspends = candidate.type == "await_expression" or (
                candidate.type == "for_in_statement" and any(child.type == "await" for child in candidate.children)
            )
            if suspends and statement.start_byte <= candidate.start_byte < node.start_byte:
                if not candidate.start_byte <= node.start_byte < candidate.end_byte:
                    raise ValueError("suspended store")
            if candidate.type in {"break_statement", "continue_statement"} and any(
                loop.start_byte <= candidate.start_byte < loop.end_byte for loop in loops
            ):
                raise ValueError("store control flow")
        snapshot = (arguments, key)
        if snapshot not in self.snapshots:
            self.snapshots[snapshot] = None
            args = children(arguments)
            values = [
                self.data.value(args[1], row)
                for row in self.data.environments(arguments, strict=True)
                if self.data.value(args[0], row) == key
            ]
            if len(values) == 1:
                self.snapshots[snapshot] = values[0]
        value = self.snapshots[snapshot]
        if value is None:
            raise ValueError("unknown stored value")
        return value

    def _value(self, node: Node, environment: dict) -> object:
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            if function.type == "identifier" and source(function) == "load":
                args = children(node.child_by_field_name("arguments"))
                if len(args) != 1:
                    raise ValueError("load arguments")
                key = self.data.value(args[0], environment)
                if type(key) is not str:
                    raise ValueError("load key")
                key.encode()
                return self.stored_value(node, key)
        return super()._value(node, environment)


def literal_commands(
    arguments: Node,
    shell_word: re.Pattern,
    count_limit: int,
    byte_limit: int,
    *,
    occurrences: bool = False,
    reader: LiteralData | None = None,
) -> list[str] | None:
    try:
        return (reader or LiteralCommands(shell_word, count_limit, byte_limit)).commands(arguments, occurrences=occurrences)
    except (ValueError, KeyError, SyntaxError, RecursionError):
        return None


EXEC_OPTIONS = {
    "cmd",
    "workdir",
    "yield_time_ms",
    "max_output_tokens",
    "shell",
    "login",
    "tty",
    "timeout_ms",
    "sandbox_permissions",
    "additional_permissions",
    "justification",
    "prefix_rule",
    "environment_id",
}


def shell_method(arguments: Node) -> str:
    return property_name(arguments.parent.child_by_field_name("function").child_by_field_name("property"))


def closed_stdin(arguments: Node) -> bool:
    try:
        method = shell_method(arguments)
        if method not in {"exec_command", "write_stdin"}:
            return False
        objects = children(arguments)
        if len(objects) != 1 or objects[0].type != "object":
            return False
        names = set()
        for member in children(objects[0]):
            key = member.child_by_field_name("key") if member.type == "pair" else member
            name = property_name(key)
            options = EXEC_OPTIONS if method == "exec_command" else {"session_id", "chars", "yield_time_ms", "max_output_tokens"}
            if name in names or name not in options or (key.type == "string" and "\\" in source(key)):
                return False
            names.add(name)
            if name == "tty" and (member.type != "pair" or member.child_by_field_name("value").type != "false"):
                return False
            if name == "chars" and (member.type != "pair" or literal_string(member.child_by_field_name("value")) != ""):
                return False
        return True
    except (AttributeError, ValueError):
        return False


def analyse(text: str, *, refusals: list[dict] | None = None) -> tuple[bool, list[tuple[Node, bool]]]:
    import tree_sitter_javascript

    data = text.replace("\r\n", "\n").replace("\r", "\n").encode()
    root = Parser(Language(tree_sitter_javascript.language())).parse(data).root_node
    sites = []
    inert = []
    lexical = True
    invalid = None
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(reversed(node.named_children))
        if node.is_missing:
            lexical = False
        if invalid is None and (node.is_missing or node.type == "ERROR"):
            invalid = node
        if node.type == "regex" and source(node).startswith(("/*", "//")):
            lexical = False
            invalid = invalid or node
        if node.type in {"string", "string_fragment", "escape_sequence", "comment", "hash_bang_line", "regex"}:
            if node.type == "comment" and source(node).startswith(("<!--", "-->")):
                lexical = False
            if node.type == "hash_bang_line" and node.start_byte != 0:
                lexical = False
            inert.append((node.start_byte, node.end_byte))
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            continue
        receiver = callee.child_by_field_name("object")
        method = source(callee.child_by_field_name("property"))
        if receiver.type == "identifier" and source(receiver) == "tools" and method in SHELL_TOOLS:
            sites.append((node.child_by_field_name("arguments"), method == "write_stdin"))
    if not lexical or root.has_error or "\u2028" in text or "\u2029" in text:
        if refusals is not None:
            boundary = next((data.index(marker) for marker in (b"\xe2\x80\xa8", b"\xe2\x80\xa9") if marker in data), None)
            refusals.append(refusal(invalid or root, "JavaScript syntax or lexical boundary", offset=boundary))
        return False, sites
    visible = bytearray(data)
    for start, end in inert:
        visible[start:end] = bytes(value if value in {10, 13} else 32 for value in data[start:end])
    if b"\\" in visible or b"<!--" in visible or b"#!" in visible or b"-->" in visible:
        if refusals is not None:
            offset = min(visible.index(marker) for marker in (b"\\", b"<!--", b"#!", b"-->") if marker in visible)
            refusals.append(refusal(root, "JavaScript lexical boundary", offset=offset))
        return False, sites
    try:
        checker = JavascriptData()
        checker.statement(root, checker.environment)
    except (KeyError, ValueError, SyntaxError, RecursionError) as error:
        if refusals is not None:
            refusals.append(refusal(getattr(error, "node", None) or root, f"{type(error).__name__}: {error}"))
        return False, sites
    return True, sites
