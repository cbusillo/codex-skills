# /// script
# requires-python = ">=3.12"
# dependencies = ["tree-sitter==0.26.0", "tree-sitter-javascript==0.25.0"]
# ///
from __future__ import annotations

import ast
import re

from tree_sitter import Language, Node, Parser

NATIVE_NAMES = {"tools", "text", "store", "load", "exit", "Promise", "JSON", "Object", "ALL_TOOLS", "undefined"}
SHELL_TOOLS = {"exec_command", "shell_command", "shell", "write_stdin"}
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


def children(node: Node) -> list[Node]:
    return [child for child in node.named_children if child.type != "comment"]


def source(node: Node) -> str:
    return node.text.decode()


def property_name(node: Node) -> str:
    value = source(node)
    if node.type == "string":
        if re.fullmatch(r"(?:[^\\]|\\(?:[\\\"'nrtbfv]|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}))*", value) is None:
            raise ValueError(value)
        value = ast.literal_eval(value)
    elif node.type not in {"identifier", "property_identifier"} or re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value) is None:
        raise ValueError(value)
    if value in PROTOTYPE_NAMES:
        raise ValueError(value)
    return value


class JavascriptData:
    def __init__(self) -> None:
        self.environment: dict[str, tuple[str, bool]] = {"ALL_TOOLS": ("data", False), "undefined": ("data", False)}

    def bind(self, node: Node, environment: dict, *, kind: str = "data", mutable: bool = False) -> None:
        if node.type == "identifier":
            name = property_name(node)
            if name in NATIVE_NAMES:
                raise ValueError(name)
            environment[name] = (kind, mutable)
        elif node.type in {"array_pattern", "formal_parameters"}:
            for child in children(node):
                self.bind(child, environment)
        else:
            raise ValueError(node.type)

    def expression(self, node: Node, environment: dict) -> str:
        kind = node.type
        if kind in {"identifier", "shorthand_property_identifier"}:
            return environment[source(node)][0]
        if kind == "number":
            return "number"
        if kind in {"string", "true", "false", "null", "undefined"}:
            return "data"
        if kind in {"member_expression", "subscript_expression"}:
            self.expression(node.child_by_field_name("object"), environment)
            member = node.child_by_field_name("property" if kind == "member_expression" else "index")
            if kind == "member_expression" or member.type == "string":
                property_name(member)
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
            if kind in {"parenthesized_expression", "unary_expression"} and results == ["number"]:
                return "number"
            return "data"
        raise ValueError(kind)

    def call(self, node: Node, environment: dict) -> str:
        callee = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if arguments.type != "arguments" or any(child.type == "optional_chain" for child in node.children):
            raise ValueError("call")
        args = children(arguments)
        if any(argument.type == "spread_element" for argument in args):
            raise ValueError("spread arguments")
        if callee.type == "identifier" and source(callee) in {"text", "store", "load", "exit"}:
            pass
        elif callee.type == "member_expression":
            receiver = callee.child_by_field_name("object")
            method = property_name(callee.child_by_field_name("property"))
            native = source(receiver) if receiver.type == "identifier" else ""
            if native == "tools" and method in SHELL_TOOLS | {"apply_patch", "clock__curr_time"}:
                pass
            elif native == "Promise" and method in {"all", "allSettled"} and len(args) == 1:
                pass
            elif native == "JSON" and method in {"parse", "stringify"} and len(args) == 1:
                pass
            elif native == "Object" and method in {"keys", "values", "entries"} and len(args) == 1:
                pass
            else:
                self.expression(receiver, environment)
                if method in CALLBACK_METHODS and len(args) == 1:
                    callback = args[0]
                    if method == "forEach" and callback.type == "identifier" and source(callback) == "text":
                        return "data"
                    if callback.type != "arrow_function":
                        raise ValueError("callback")
                    local = environment.copy()
                    parameters = callback.child_by_field_name("parameter") or callback.child_by_field_name("parameters")
                    if parameters is not None:
                        self.bind(parameters, local)
                    body = callback.child_by_field_name("body")
                    if body.type == "statement_block":
                        self.statement(body, local)
                    else:
                        self.expression(body, local)
                    return "data"
                if method not in DATA_METHODS:
                    raise ValueError(method)
        else:
            raise ValueError("callee")
        for argument in args:
            self.expression(argument, environment)
        return "data"

    def statement(self, node: Node, environment: dict) -> None:
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


def analyse(text: str) -> tuple[bool, list[tuple[int, bool]]]:
    import tree_sitter_javascript

    data = text.encode()
    root = Parser(Language(tree_sitter_javascript.language())).parse(data).root_node
    sites = []
    inert = []
    lexical = True
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(reversed(node.named_children))
        if node.type in {"string", "string_fragment", "escape_sequence", "comment", "hash_bang_line"}:
            if node.type == "comment" and source(node).startswith(("<!--", "-->")):
                lexical = False
            if node.type == "hash_bang_line" and node.start_byte != 0:
                lexical = False
            inert.append((node.start_byte, node.end_byte))
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        if callee.type != "member_expression":
            continue
        receiver = callee.child_by_field_name("object")
        method = source(callee.child_by_field_name("property"))
        if receiver.type == "identifier" and source(receiver) == "tools" and method in SHELL_TOOLS:
            start = node.child_by_field_name("arguments").start_byte + 1
            sites.append((len(data[:start].decode()), method == "write_stdin"))
    if not lexical or root.has_error or "\u2028" in text or "\u2029" in text:
        return False, sites
    visible = bytearray(data)
    for start, end in inert:
        visible[start:end] = b" " * (end - start)
    if b"\\" in visible or b"<!--" in visible or b"#!" in visible or re.search(rb"(?m)^[ \t]*-->", visible):
        return False, sites
    try:
        checker = JavascriptData()
        checker.statement(root, checker.environment)
    except (KeyError, ValueError, RecursionError):
        return False, sites
    return True, sites
