#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Warn about the five recorded change-detector patterns in tracked tests."""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".swift"}
VERSION = re.compile(r"(?<![\w.])(?:>=\s*)?\d+\.\d+\.\d+(?:[-+][\w.]+)?(?![\w.])|@v\d+\b")
STRING = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`''')
READ = re.compile(r"(?:^|\.)(?:open|read_text|read_bytes|read|readFileSync|readFile|readText)$|(?:^|\.)load\w*", re.I)
TEMP = re.compile(r"\b(?:tmp_path|tmp_path_factory|tmpdir|temp_dir|temp_root|temporary_directory|tempfile|TemporaryDirectory|mkdtemp|mktemp|temp)\b", re.I)
EXECUTE = re.compile(r"subprocess\.|os\.system|(?:^|\.)(?:run|execute|exec|eval|run_shell)\b")
MESSAGES = {
    "pinned-config": "Tracked config value compared with a test-owned literal; enforce the rule in a linter or compare with its source of truth.",
    "workflow-no-execution": "Tracked workflow loaded without observable step execution in this test; inspect whether this is lint-in-test.",
    "source-text": "Literal asserted against tracked source text; test behavior or move import rules to a linter.",
    "duplicated-version": "Asserted version literal also appears in a tracked non-test file; use its source of truth.",
    "instruction-text": "Catalog instruction text asserted as a string; validate behavior instead.",
    "parse-error": "Test could not be parsed; quality coverage is incomplete.",
}


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str
    classification: str
    message: str


def finding(path: str, line: int, rule: str) -> Finding:
    classification = "warning" if rule in {"workflow-no-execution", "parse-error"} else "hard-failure-candidate"
    return Finding(path, line, rule, classification, MESSAGES[rule])


def is_test(path: str) -> bool:
    file = Path(path)
    return file.suffix in EXTENSIONS and (
        any(part.lower() in {"test", "tests", "__tests__"} or part.endswith("Tests")
            for part in file.parts[:-1])
        or file.name.startswith("test_") or file.stem.endswith(("_test", "Tests", "Test"))
        or ".test." in file.name or ".spec." in file.name
    )


def kind(path: str, catalog: bool) -> str | None:
    file = Path(path)
    if any(part.lower() in {"fixtures", "test-fixtures"} for part in file.parts):
        return None
    if path.startswith(".github/") or file.name in {"pyproject.toml", "package.json"} or (
        file.name.startswith("Dockerfile") or fnmatch.fnmatch(file.name, "docker-compose*.yml")
        or file.suffix in {".lock", ".entitlements"} or file.name.endswith("Info.plist")
        or file.name in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Package.resolved"}
    ):
        return "config"
    if catalog and (file.name in {"SKILL.md", "AGENTS.md"} or (
        ("references" in file.parts or file.parts[0] == "instructions") and file.suffix == ".md"
    )):
        return "instruction"
    return "source" if file.suffix in {".py", ".js", ".sh"} else None


def match_paths(values: list[str], tracked: set[str]) -> set[str]:
    value = str(Path("/".join(values))) if values else ""
    value = re.sub(r"^(?:\./|\.\./)+", "", value)
    matches = {path for path in tracked if value and (
        path == value or (Path(value).is_absolute() and value.endswith("/" + path))
        or fnmatch.fnmatchcase(path, value)
    )}
    # An absolute fixture path must not also resolve to a shorter root filename.
    longest = max((len(path) for path in matches), default=0)
    return {path for path in matches if len(path) == longest}


def aliases_for(nodes: list[ast.AST]) -> dict[str, ast.AST]:
    aliases: dict[str, ast.AST] = {}
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(node.value, (ast.Tuple, ast.List)):
                        for left, right in zip(target.elts, node.value.elts):
                            aliases[ast.unparse(left)] = right
                    elif isinstance(target, (ast.Tuple, ast.List)):
                        for left in target.elts:
                            aliases[ast.unparse(left)] = node.value
                    else:
                        aliases[ast.unparse(target)] = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                aliases[ast.unparse(node.target)] = node.value
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                aliases[ast.unparse(node.target)] = node.iter
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        aliases[ast.unparse(item.optional_vars)] = item.context_expr
    return aliases


def python_findings(path: str, source: str, tracked: set[str], versions: set[str], catalog: bool) -> list[Finding]:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [finding(path, error.lineno or 1, "parse-error")]
    module_aliases = aliases_for([node for node in tree.body if not isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    )])
    scopes: list[tuple[ast.AST, dict[str, ast.AST], dict[str, ast.AST]]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append((node, module_aliases, {}))
        elif isinstance(node, ast.ClassDef):
            setup = [child for child in node.body if isinstance(child, ast.FunctionDef)
                     and child.name in {"setUp", "setUpClass"}]
            baseline = {**module_aliases, **aliases_for(setup)}
            methods: dict[str, ast.AST] = {child.name: child for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))}
            scopes.extend((child, baseline, methods) for child in methods.values())
    # Top-level pytest assertions also count.
    scopes.append((ast.Module(body=[node for node in tree.body if not isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))], type_ignores=[]), module_aliases, {}))

    def executes(scope: ast.AST, methods: dict[str, ast.AST], seen: frozenset[int] = frozenset()) -> bool:
        # A step run by a method of the same class (self.helper(...)) is still executed. Only
        # self/cls attribute calls are followed: a local name cannot rebind them, so no symbol
        # table is needed. Bare module-function helpers stay a documented false positive.
        for call in (item for item in ast.walk(scope) if isinstance(item, ast.Call)):
            if EXECUTE.search(ast.unparse(call.func)):
                return True
            if isinstance(call.func, ast.Attribute) and ast.unparse(call.func.value) in {"self", "cls"}:
                target = methods.get(call.func.attr)
                if target is not None and id(target) not in seen and executes(target, methods, seen | {id(scope)}):
                    return True
        return False

    def fixture_executed(scope: ast.AST, methods: dict[str, ast.AST]) -> bool:
        # A fixture is executed only by a method that runs and reads what the fixture stored.
        stored = {node.attr for node in ast.walk(scope) if isinstance(node, ast.Attribute)
                  and isinstance(node.ctx, ast.Store) and ast.unparse(node.value) in {"self", "cls"}}
        return any(method is not scope and executes(method, methods) and any(
            isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in stored
            and ast.unparse(node.value) in {"self", "cls"} for node in ast.walk(method))
            for method in methods.values())

    found: set[Finding] = set()
    for scope, baseline, methods in scopes:
        aliases = {**baseline, **aliases_for([scope])}

        def expanded(node: ast.AST, seen: frozenset[str] = frozenset()) -> list[ast.AST]:
            key = ast.unparse(node) if isinstance(node, (ast.Name, ast.Attribute)) else ""
            if key in aliases and key not in seen:
                return expanded(aliases[key], seen | {key})
            return [node, *(item for child in ast.iter_child_nodes(node) for item in expanded(child, seen))]

        def strings(node: ast.AST) -> list[str]:
            return [item.value for item in expanded(node)
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)]

        def origins(node: ast.AST) -> set[str]:
            def follow(item: ast.AST, seen: frozenset[str] = frozenset()) -> set[str]:
                key = ast.unparse(item) if isinstance(item, (ast.Name, ast.Attribute)) else ""
                if key in aliases and key not in seen:
                    return follow(aliases[key], seen | {key})
                if isinstance(item, ast.Call):
                    name = ast.unparse(item.func)
                    # Running a step produces behavior output, not config text.
                    if EXECUTE.search(name):
                        return set()
                    if name == "inspect.getsource":
                        return {"<inspect-source>"}
                    decoder = name.startswith(("json.", "tomllib.", "yaml.", "plistlib."))
                    if READ.search(name) and not decoder:
                        path_node = item.func.value if isinstance(item.func, ast.Attribute) and item.func.attr in {
                            "read_text", "read_bytes", "read", "open"
                        } else (item.args[0] if item.args else item)
                        expanded_text = " ".join(ast.unparse(child) for child in expanded(path_node))
                        if not TEMP.search(expanded_text):
                            paths = match_paths(strings(path_node), tracked)
                            if paths:
                                return paths
                        if path_node is not item:
                            return follow(path_node, seen)
                        return set()
                    if not decoder and not (isinstance(item.func, ast.Attribute) and item.func.attr in {
                        "split", "strip", "decode", "replace", "get", "items", "values"
                    }):
                        return set()
                return set().union(*(follow(child, seen) for child in ast.iter_child_nodes(item)))
            return follow(node)

        fixture = getattr(scope, "name", "") in {"setUp", "setUpClass"}
        executing = executes(scope, methods) or (fixture and fixture_executed(scope, methods))
        for node in ast.walk(scope):
            if isinstance(node, ast.Call) and not executing and READ.search(ast.unparse(node.func)) and not ast.unparse(node.func).startswith(("json.", "tomllib.", "yaml.", "plistlib.")):
                for origin in origins(node):
                    if origin.startswith(".github/workflows/") and origin.endswith((".yml", ".yaml")):
                        found.add(finding(path, node.lineno, "workflow-no-execution"))
            operands: list[ast.AST] = []
            membership = False
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"assertEqual", "assertNotEqual", "assertIn", "assertNotIn", "assertRegex", "assertMultiLineEqual"}:
                    operands = node.args[:2]
                    membership = node.func.attr in {"assertIn", "assertNotIn", "assertRegex"}
            elif isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                membership = any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
            if not operands:
                continue
            def expected_strings(item: ast.AST, seen: frozenset[str] = frozenset()) -> list[str]:
                if isinstance(item, ast.Name) and item.id in aliases and item.id not in seen:
                    return expected_strings(aliases[item.id], seen | {item.id})
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    return [item.value]
                if isinstance(item, (ast.Tuple, ast.List, ast.Set)):
                    return [value for child in item.elts for value in expected_strings(child, seen)]
                return []
            # Subscript keys are identifiers, not expected literal operands.
            literals = [value for operand in operands for value in expected_strings(operand)]
            if not literals:
                continue
            inputs = set().union(*(origins(operand) for operand in operands))
            kinds = {kind(origin, catalog) for origin in inputs}
            if "config" in kinds and not membership:
                found.add(finding(path, node.lineno, "pinned-config"))
            if membership and ("source" in kinds or "<inspect-source>" in inputs):
                found.add(finding(path, node.lineno, "source-text"))
            if "instruction" in kinds:
                found.add(finding(path, node.lineno, "instruction-text"))
            if any(token in versions for value in literals for token in VERSION.findall(value)):
                found.add(finding(path, node.lineno, "duplicated-version"))
    return sorted(found)


def foreign_findings(path: str, source: str, tracked: set[str], versions: set[str], catalog: bool) -> list[Finding]:
    tokens = re.compile(STRING.pattern + r"|//[^\n]*|/\*[\s\S]*?\*/")
    cleaned = tokens.sub(lambda match: match.group() if STRING.fullmatch(match.group())
                         else re.sub(r"[^\n]", " ", match.group()), source)
    quoted = {item.start(): item.end() for item in STRING.finditer(cleaned)}
    calls: list[tuple[int, int, str, str]] = []
    for match in re.finditer(r"(?:\b\w+|#expect|#require)\s*\(", cleaned):
        if any(start <= match.start() < end for start, end in quoted.items()):
            continue
        depth, end = 1, match.end()
        while end < len(cleaned) and depth:
            if end in quoted:
                end = quoted[end]
                continue
            depth += (cleaned[end] == "(") - (cleaned[end] == ")")
            end += 1
        calls.append((match.start(), end, match.group().split("(")[0].strip(), cleaned[match.end():end - 1]))
    # Bounded foreign-language data flow: reader calls plus let/const/var aliases.
    config_names: set[str] = set()
    found: set[Finding] = set()
    config_paths = {item for literal in STRING.finditer(cleaned)
                    for item in match_paths([literal.group()[1:-1]], tracked)
                    if kind(item, catalog) == "config"}
    for match in re.finditer(r'''import\s+(\w+)\s+from\s+(["'])(.*?)\2''', cleaned):
        if match_paths([match.group(3)], tracked) & config_paths:
            config_names.add(match.group(1))
    for start, _end, name, body in calls:
        if re.search(r"readFile|readText|load|String|^require$", name, re.I):
            values = [m.group()[1:-1] for m in STRING.finditer(body)]
            paths = match_paths(values[:1], tracked)
            if (paths & config_paths or (config_paths and name.startswith("load") and re.search(r"\bpath\b", body))) and not TEMP.search(body):
                prefix = cleaned[max(0, cleaned.rfind("\n", 0, start)):start]
                assignment = re.search(r"(?:let|const|var)\s+(\w+)\s*=", prefix)
                if assignment:
                    config_names.add(assignment.group(1))
                for origin in paths:
                    if origin.startswith(".github/workflows/") and not EXECUTE.search(cleaned):
                        found.add(finding(path, cleaned.count("\n", 0, start) + 1, "workflow-no-execution"))
    for _ in range(3):
        for match in re.finditer(r"(?:let|const|var)\s+(\w+)\s*=([^;\n]+)", cleaned):
            expression = STRING.sub("", match.group(2))
            if any(re.search(rf"\b{re.escape(name)}\b", expression) for name in config_names):
                config_names.add(match.group(1))
    for start, _end, name, body in calls:
        node_assertion = name in {"equal", "strictEqual", "deepStrictEqual"} and cleaned[:start].rstrip().endswith("assert.")
        if not node_assertion and not re.search(r"assert|expect|toEqual|toBe|toStrictEqual|toContain|toMatch", name, re.I):
            continue
        expected = body.split("==", 1)[-1] if "==" in body else body
        values = [item.group()[1:-1] for item in STRING.finditer(expected)]
        if not values:
            continue
        expression = STRING.sub("", body)
        if name in {"toEqual", "toBe", "toStrictEqual", "toContain", "toMatch"}:
            for _prior_start, prior_end, prior_name, prior_body in reversed(calls):
                if prior_end <= start and prior_name == "expect" and cleaned[prior_end:start].strip() in {".", ".not."}:
                    expression += " " + STRING.sub("", prior_body)
                    break
        line = cleaned.count("\n", 0, start) + 1
        if name not in {"toContain", "toMatch"} and any(re.search(rf"\b{re.escape(alias)}\b", expression) for alias in config_names):
            found.add(finding(path, line, "pinned-config"))
        if any(token in versions for value in values for token in VERSION.findall(value)):
            found.add(finding(path, line, "duplicated-version"))
    return sorted(found)


def scan(repo: Path, excludes: list[str]) -> list[Finding]:
    paths = subprocess.run(["git", "ls-files", "-z"], cwd=repo, check=True,
                           capture_output=True).stdout.decode().split("\0")
    tracked = set(paths) - {""}
    catalog = {"scripts/check_test_quality.py", "skills/references/model-review.md"} <= tracked
    versions: set[str] = set()
    sources: dict[str, str] = {}
    for path in sorted(tracked):
        file = repo / path
        if file.is_symlink() or not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if is_test(path):
            sources[path] = text
        else:
            versions.update(VERSION.findall(text))
    found = []
    for path, source in sources.items():
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in excludes):
            continue
        parser = python_findings if Path(path).suffix == ".py" else foreign_findings
        found.extend(parser(path, source, tracked, versions, catalog))
    return sorted(found)


def escape(value: str, *, property_value: bool = False) -> str:
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return value.replace(",", "%2C").replace(":", "%3A") if property_value else value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--format", choices=("text", "github", "json"), default="text")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    args = parser.parse_args(argv)
    try:
        findings = scan(args.repo.resolve(), args.exclude)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Test-quality scan failed: {error}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps({"mode": "warning", "findings": [asdict(item) for item in findings]}))
    else:
        for item in findings:
            message = f"[{item.classification}] {item.message}"
            if args.format == "github":
                print(f"::warning file={escape(item.path, property_value=True)},line={item.line},"
                      f"title=Test quality ({item.rule})::{escape(message)}")
            else:
                print(f"{item.path}:{item.line}: warning [{item.rule}] {message}")
        print(f"Test quality: {len(findings)} review candidate(s); warning only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
