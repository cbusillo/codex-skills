#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Pure selected-file execution coverage and independent defect canaries.

The caller separately proves native completion, unchanged inputs, exact scope
traversal and cleanup. Findings never substitute for selected-file trace pairs.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

CANARY_FILES = {
    "DuplicatedCode": "duplicates.py",
    "HttpUrlsUsage": "coverage.py",
    "PyUnresolvedReferencesInspection": "coverage.py",
    "PyTypeCheckerInspection": "python_types.py",
    "OdooPy": "models.py",
    "OdooXml": "views/defect.xml",
}
# JetBrains PyUnusedSymbolInspectionMerger migrates the retired combined tool
# into all three successors. The original profile remains unchanged.
UNUSED_SPLIT = frozenset({
    "PyUnusedLocalVariableInspection", "PyUnusedParameterInspection", "PyUnusedFunctionInspection",
})
UNIVERSAL_TOOLS = frozenset({
    "DuplicatedCode", "HttpUrlsUsage", "SpellCheckingInspection", "InconsistentLineSeparators",
    "IncorrectFormatting", "ProblematicWhitespace", "TodoComment",
})

STYLESHEET_SUFFIXES = frozenset({".css", ".scss", ".sass", ".less"})
SCRIPT_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts", ".vue"})
MARKUP_SUFFIXES = frozenset({".html", ".htm", ".xhtml", ".vue"})
JVM_SUFFIXES = frozenset({".java", ".kt", ".kts"})
UNPROVEN_LANGUAGE_SUFFIXES = {
    "css": STYLESHEET_SUFFIXES, "scss": STYLESHEET_SUFFIXES, "sass": STYLESHEET_SUFFIXES, "less": STYLESHEET_SUFFIXES,
    "javascript": SCRIPT_SUFFIXES, "typescript": SCRIPT_SUFFIXES, "html": MARKUP_SUFFIXES,
    "json": frozenset({".json", ".json5", ".jsonc"}), "sql": frozenset({".sql"}), "java": JVM_SUFFIXES, "kotlin": JVM_SUFFIXES,
}


def migrate_expected(requested: Iterable[str], registered: Iterable[str]) -> tuple[set[str], dict[str, list[str]]]:
    expected, registered = set(requested), set(registered)
    migrations = {}
    if "PyUnusedLocalInspection" in expected and "PyUnusedLocalInspection" not in registered and UNUSED_SPLIT <= registered:
        expected.remove("PyUnusedLocalInspection")
        expected.update(UNUSED_SPLIT)
        migrations["PyUnusedLocalInspection"] = sorted(UNUSED_SPLIT)
    return expected, migrations


def applicable_files(tool: str, selected: set[str], language: str | None = None, *, scope: str = "files") -> set[str] | None:
    """None means unknown applicability, never permission to omit the tool."""
    language = language.casefold() if isinstance(language, str) else None
    if tool in UNIVERSAL_TOOLS:
        return set(selected)
    if tool == "OdooPy" or tool.startswith("Py"):
        return {path for path in selected if path.endswith(".py")}
    if tool == "OdooXml" or tool.startswith("Xml"):
        return {path for path in selected if path.endswith(".xml")}
    if language == "python":
        return {path for path in selected if path.endswith(".py")}
    if language == "xml":
        return {path for path in selected if path.endswith(".xml")}
    if language == "yaml" or tool.startswith("YAML"):
        return {path for path in selected if Path(path).suffix in {".yaml", ".yml"}}
    if tool == "EmptyDirectory" and scope in {"files", "changed_files"}:
        return set()
    # These established profile families can be absent from PyCharm's registry;
    # their applicability does not depend on registration succeeding.
    if tool.startswith("Css"):
        return unproven_when_selected(selected, STYLESHEET_SUFFIXES)
    if tool == "Eslint":
        return unproven_when_selected(selected, SCRIPT_SUFFIXES)
    if language in UNPROVEN_LANGUAGE_SUFFIXES:
        return unproven_when_selected(selected, UNPROVEN_LANGUAGE_SUFFIXES[language])
    return None


def unproven_when_selected(selected: set[str], suffixes: frozenset[str]) -> set[str] | None:
    if any(Path(path).suffix.casefold() in suffixes for path in selected):
        return None
    return set()


def assess(
    requested: Iterable[str], registered: Iterable[str], trace_tools: Mapping[str, set[str]],
    trace_failed: Iterable[tuple[str, str]], selected: Iterable[str],
    control_findings: Iterable[Mapping[str, Any]], *, control_root: str,
    tool_languages: Mapping[str, str] | None = None,
    scope: str = "files",
) -> tuple[dict[str, Any], list[str]]:
    """Assess selected execution and current-run canaries without side effects.

    ``requested`` includes enabled profile tools and required named checks.
    ``registered`` proves availability only. ``trace_tools`` contains completed
    physical tool/file pairs from this run. ``control_findings`` contains its
    raw finding identities; ``control_root`` pins their exact per-run directory.
    """
    profile_requested, registered, selected = set(requested), set(registered), set(selected)
    effective, migrations = migrate_expected(profile_requested, registered)
    failed = set(trace_failed)
    languages = tool_languages or {}
    required, not_applicable, unknown = {}, set(), set()
    for tool in sorted(effective):
        files = applicable_files(tool, selected, languages.get(tool), scope=scope)
        if files is None:
            unknown.add(tool)
        elif files:
            required[tool] = files
        else:
            not_applicable.add(tool)

    selected_execution, missing_execution = {}, {}
    for tool, files in required.items():
        proved = files & set(trace_tools.get(tool, set()))
        proved -= {path for failed_tool, path in failed if failed_tool == tool}
        selected_execution[tool] = sorted(proved)
        if files - proved:
            missing_execution[tool] = sorted(files - proved)

    finding_pairs = {(finding.get("rule_id"), finding.get("file")) for finding in control_findings}
    canaries = {}
    for tool in sorted(set(required) & CANARY_FILES.keys()):
        path = control_root + "/" + CANARY_FILES[tool]
        detected = (tool, path) in finding_pairs and (tool, path) not in failed
        canaries[tool] = {"file": path, "detected": detected}

    active = set(required) | unknown
    unavailable = active - registered
    unproven = set(missing_execution) | unknown
    gaps = ["execution_unproven:" + tool for tool in sorted(unproven)]
    gaps += ["inspection_unavailable:" + tool for tool in sorted(unavailable)]
    gaps += ["inspection_applicability_unproven:" + tool for tool in sorted(unknown)]
    gaps += ["canary_unproven:" + tool for tool, result in canaries.items() if not result["detected"]]
    control_paths = {result["file"] for result in canaries.values()}
    gaps += ["inspection_execution_failed:" + tool + ":" + path
             for tool, path in sorted(failed) if path in selected or path in control_paths]
    if not selected:
        gaps.append("native_scope_empty")
    elif not active:
        gaps.append("native_applicable_tools_empty")
    return {
        "requested": sorted(active), "profile_requested": sorted(profile_requested),
        "registered": sorted(active & registered),
        "execution_proved": sorted(set(required) - set(missing_execution)),
        "selected_execution": selected_execution, "missing_execution": missing_execution,
        "canary_results": canaries, "not_applicable": sorted(not_applicable),
        "applicability_unproven": sorted(unknown), "vendor_migrations": migrations,
        "unavailable": sorted(unavailable), "unproven": sorted(unproven), "complete": not gaps,
    }, gaps


def trace_evidence(payload: Any, project: Path) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """Parse completed stock Jaeger spans; failed pairs cannot borrow success."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("native_trace_invalid")
    success: dict[str, set[str]] = {}
    failed: set[tuple[str, str]] = set()
    for batch in payload["data"]:
        if not isinstance(batch, dict) or not isinstance(batch.get("spans"), list):
            raise ValueError("native_trace_invalid")
        for span in batch["spans"]:
            if not isinstance(span, dict):
                raise ValueError("native_trace_invalid")
            if span.get("operationName") != "inspectionRun":
                continue
            tags = {}
            raw_tags = span.get("tags", [])
            if not isinstance(raw_tags, list):
                raise ValueError("native_trace_invalid")
            for tag in raw_tags:
                if not isinstance(tag, dict) or not isinstance(tag.get("key"), str) or tag["key"] in tags:
                    raise ValueError("native_trace_invalid")
                tags[tag["key"]] = tag.get("value")
            tool, raw = tags.get("inspectionId"), tags.get("file")
            if not isinstance(tool, str) or not tool:
                raise ValueError("native_trace_invalid")
            if not isinstance(raw, str):
                continue
            source = Path(raw)
            if not source.is_absolute() or ".." in source.parts or not source.is_relative_to(project):
                continue
            relative = source.relative_to(project).as_posix()
            if tags.get("otel.status_code") == "ERROR" or str(tags.get("error")).lower() == "true":
                failed.add((tool, relative))
                continue
            duration = span.get("durationNano", span.get("duration"))
            if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
                raise ValueError("native_trace_invalid")
            success.setdefault(tool, set()).add(relative)
    for tool, path in failed:
        success.get(tool, set()).discard(path)
    return success, failed


def read_trace(path: Path, project: Path) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    if path.is_symlink():
        raise ValueError("native_trace_unsafe")
    return trace_evidence(json.loads(path.read_text()), project)
