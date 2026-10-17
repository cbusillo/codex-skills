#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Reject disabled TLS verification, including hand-built SSL contexts."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import subprocess
import sys


def qualified(node: ast.AST, imports: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return imports.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        name = qualified(node.value, imports) + "." + node.attr
        return imports.get(name, name)
    if isinstance(node, ast.Call):
        return qualified(node.func, imports)
    return ""


def disabled(node: ast.AST, imports: dict[str, str]) -> bool:
    if isinstance(node, ast.IfExp):
        return disabled(node.body, imports) or disabled(node.orelse, imports)
    return qualified(node, imports) in {"ssl.CERT_NONE", "ssl.VerifyMode.CERT_NONE"} or isinstance(node, ast.Constant) and node.value in {"CERT_NONE", "NONE"}


def literal_false(node: ast.AST) -> bool:
    if isinstance(node, ast.IfExp):
        return literal_false(node.body) or literal_false(node.orelse)
    return isinstance(node, ast.Constant) and node.value is False


def unsafe(node: ast.AST, imports: dict[str, str]) -> bool:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if value is not None and qualified(value, imports) in {"ssl._create_unverified_context", "ssl._create_stdlib_context"}:
            return True
        if isinstance(value, (ast.Tuple, ast.List)):
            for target in targets:
                if isinstance(target, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
                    if any(unsafe(ast.Assign(targets=[part], value=part_value), imports) for part, part_value in zip(target.elts, value.elts)):
                        return True
        return any(isinstance(target, ast.Attribute) and (
            target.attr in {"check_hostname", "verify"} and value is not None and literal_false(value)
            or target.attr == "verify_mode" and value is not None and disabled(value, imports)
        ) for target in targets)
    if isinstance(node, ast.Call):
        name = qualified(node.func, imports)
        if name in {"ssl._create_unverified_context", "ssl._create_stdlib_context"}:
            return True
        if name.split(".")[0] in {"requests", "httpx", "aiohttp", "urllib3"}:
            return any(
                keyword.arg in {"verify", "ssl", "verify_ssl", "assert_hostname"} and literal_false(keyword.value)
                or keyword.arg == "cert_reqs" and disabled(keyword.value, imports)
                for keyword in node.keywords
            )
    return False


def has_reason(lines: list[str], line: int, column: int, assignment_ends: dict[tuple[int, int], int]) -> bool:
    """One comment covers adjacent unsafe statements at the same choke point."""
    comments = []
    index = line - 2
    while index >= 0:
        text = lines[index]
        if (index + 1, column) in assignment_ends:
            index = assignment_ends[index + 1, column] - 2
            continue
        if len(text) - len(text.lstrip()) != column or not text.lstrip().startswith("#"):
            break
        comments.append(text.lstrip()[1:].strip())
        index -= 1
    for comment in reversed(comments):
        if comment.startswith("insecure-tls:"):
            reason = comment.partition(":")[2].strip()
            return bool(reason and not re.fullmatch(r"Q\d+|https?://\S+", reason))
    return False


def findings(source: str) -> list[int]:
    tree = ast.parse(source)
    imports = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    # Also recognize ordinary client instances, without interpreting runtime code.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            name = qualified(node.value.func, imports)
            if name.split(".")[0] in {"requests", "httpx", "aiohttp", "urllib3"}:
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(target, (ast.Name, ast.Attribute)):
                        imports[qualified(target, imports)] = name
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                name = qualified(item.context_expr, imports)
                if name.split(".")[0] in {"requests", "httpx", "aiohttp", "urllib3"} and isinstance(item.optional_vars, (ast.Name, ast.Attribute)):
                    imports[qualified(item.optional_vars, imports)] = name
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    nodes = []
    for node in ast.walk(tree):
        if not unsafe(node, imports):
            continue
        while not isinstance(node, ast.stmt):
            node = parents[node]
        nodes.append(node)
    # Only adjacent attribute assignments may inherit a choke-point annotation.
    ends = {(node.end_lineno, node.col_offset): node.lineno for node in nodes
            if node.end_lineno is not None and isinstance(node, (ast.Assign, ast.AnnAssign)) and
            any(isinstance(target, ast.Attribute) and target.attr in {"check_hostname", "verify_mode", "verify"} for target in
                (node.targets if isinstance(node, ast.Assign) else [node.target]))}
    lines = source.splitlines()
    return sorted({node.lineno for node in nodes if not has_reason(lines, node.lineno, node.col_offset, ends if isinstance(node, (ast.Assign, ast.AnnAssign)) and (node.end_lineno, node.col_offset) in ends else {})})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("paths", nargs="*", type=Path, help="explicit Python files; default: tracked Python files")
    args = parser.parse_args(argv)
    try:
        paths = args.paths or [Path(path) for path in subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"], cwd=args.repo,
            check=True, capture_output=True, text=True,
        ).stdout.split("\0") if path]
        failures = 0
        for path in paths:
            file = args.repo / path
            if file.is_symlink():
                raise ValueError(f"{path}: symlinked Python input requires source qualification")
            try:
                rows = findings(file.read_text(encoding="utf-8"))
            except (OSError, UnicodeError) as error:
                raise ValueError(f"{path}: unreadable Python input") from error
            except SyntaxError:
                print(f"{path}: TLS000 cannot parse Python", file=sys.stderr)
                return 2
            for line in rows:
                print(f"{path}:{line}: TLS001 disabled TLS verification requires an insecure-tls reason at this choke point")
            failures += len(rows)
        return int(bool(failures))
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"TLS scan unavailable: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
