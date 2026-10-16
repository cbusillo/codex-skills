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
        return qualified(node.value, imports) + "." + node.attr
    return ""


def disabled(node: ast.AST, imports: dict[str, str]) -> bool:
    return qualified(node, imports) == "ssl.CERT_NONE"


def unsafe(node: ast.AST, imports: dict[str, str]) -> bool:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        return any(isinstance(target, ast.Attribute) and (
            target.attr in {"check_hostname", "verify"} and isinstance(value, ast.Constant) and value.value is False
            or target.attr == "verify_mode" and value is not None and disabled(value, imports)
        ) for target in targets)
    if isinstance(node, ast.Call):
        name = qualified(node.func, imports)
        if name == "ssl._create_unverified_context":
            return True
        if name.split(".")[0] in {"requests", "httpx", "aiohttp", "urllib3"}:
            return any(
                keyword.arg in {"verify", "ssl"} and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                or keyword.arg == "cert_reqs" and disabled(keyword.value, imports)
                for keyword in node.keywords
            )
    return False


def has_reason(lines: list[str], line: int, column: int, unsafe_ends: set[tuple[int, int]]) -> bool:
    """One comment covers adjacent unsafe statements at the same choke point."""
    comments = []
    index = line - 2
    while index >= 0:
        text = lines[index]
        if (index + 1, column) in unsafe_ends:
            index -= 1
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
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            name = qualified(node.value.func, imports)
            if name.split(".")[0] in {"requests", "httpx", "aiohttp", "urllib3"}:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        imports[target.id] = name
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    nodes = []
    for node in ast.walk(tree):
        if not unsafe(node, imports):
            continue
        while not isinstance(node, ast.stmt):
            node = parents[node]
        nodes.append(node)
    ends = {(node.end_lineno, node.col_offset) for node in nodes}
    lines = source.splitlines()
    return sorted({node.lineno for node in nodes if not has_reason(lines, node.lineno, node.col_offset, ends)})


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
                raise ValueError("symlinked Python file requires explicit source qualification")
            try:
                rows = findings(file.read_text(encoding="utf-8"))
            except SyntaxError:
                print(f"{path}: TLS000 cannot parse Python", file=sys.stderr)
                return 2
            for line in rows:
                print(f"{path}:{line}: TLS001 disabled TLS verification requires an insecure-tls reason at this choke point")
            failures += len(rows)
        return int(bool(failures))
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError):
        print("TLS scan unavailable; verify the repository and readable Python inputs", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
