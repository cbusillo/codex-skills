#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Agent-family issue routing shared by selection, claims, and creation."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Mapping

FAMILIES = ("claude", "codex")
LABEL_DEFS = {
    f"agent:{family}": {"color": "1d76db", "description": f"Assigned to the {family.title()} agent family"}
    for family in FAMILIES
}


def ancestor_agent() -> str | None:
    """Resolve inherited markers from the nearest native harness, without argv."""
    pid = os.getppid()
    seen: set[int] = set()
    deadline = time.monotonic() + 1.0
    for _ in range(12):
        remaining = deadline - time.monotonic()
        if pid <= 1 or pid in seen or remaining <= 0:
            break
        seen.add(pid)
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,comm="],
                capture_output=True, text=True, timeout=min(0.5, remaining),
            )
            if result.returncode:
                break
            parent, executable = result.stdout.strip().split(maxsplit=1)
            pid = int(parent)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            break
        name = Path(executable).name
        if name in FAMILIES:
            return name
        # Unknown native names and script-hosted agents may hide an inner
        # harness. Traverse only the shells/launchers used to run our helpers.
        if name not in {"sh", "zsh", "bash", "dash", "fish", "uv"} and not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", name):
            break
    return None


def running_agent(explicit: str | None = None, *, environ: Mapping[str, str] | None = None) -> str | None:
    if explicit is not None:
        if explicit not in FAMILIES:
            raise ValueError(f"Unknown agent family: {explicit}")
        return explicit
    env = os.environ if environ is None else environ
    codex = bool(env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID"))
    claude = bool(env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDECODE"))
    if codex != claude:
        return "codex" if codex else "claude"
    # An explicit environment requests environment-only detection, including
    # optional receipt accounting that must not add process I/O.
    return ancestor_agent() if environ is None else None


NATIVE_SESSION_VARS = {"claude": ("CLAUDE_CODE_SESSION_ID",), "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID")}


def native_sessions(agent: str | None, *, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The running family's own native session IDs by variable; empty when unreadable.

    An unidentified harness may hold only an outer harness's inherited ID, so it reads none.
    """
    env = os.environ if environ is None else environ
    return {name: env[name] for name in NATIVE_SESSION_VARS.get(agent or "", ()) if env.get(name)}


def assignment_labels(labels: list[Any]) -> list[str]:
    names = {str(item.get("name", "") if isinstance(item, dict) else item).casefold() for item in labels}
    return sorted(names.intersection(LABEL_DEFS))


def exclusion(issue: dict[str, Any], agent: str | None) -> dict[str, Any] | None:
    assigned = assignment_labels(issue.get("labels") or [])
    if not assigned or assigned == [f"agent:{agent}"]:
        return None
    return {
        **issue,
        "exclusion": "assigned_elsewhere" if agent and len(assigned) == 1 else "agent_assignment_unresolved",
        "agent_labels": assigned,
        "running_agent": agent,
    }


def creation_labels(labels: list[str], agent: str | None) -> list[str]:
    result = list(labels)
    if agent is not None:
        running_agent(agent)
        result.append(f"agent:{agent}")
    if len(assignment_labels(result)) > 1:
        raise ValueError("Choose one agent family; agent:claude and agent:codex conflict")
    return list(dict.fromkeys(result))


def filter_selection(result: dict[str, Any], agent: str | None) -> None:
    excluded = result.setdefault("excluded", [])
    candidates = []
    for item in result["candidates"]:
        mismatch = exclusion(item, agent)
        if mismatch:
            excluded.append(mismatch)
        else:
            candidates.append(item)
    result["candidates"] = candidates
    result["candidate_count"] = len(candidates)
    for rank, item in enumerate(candidates, 1):
        item["rank"] = rank
    if "available_candidates" in result:
        available = [item for item in candidates if item.get("availability") == "available"]
        result["available_candidates"] = available
        result["available_candidate_count"] = len(available)
        result["review_required_count"] = len(candidates) - len(available)
    result["running_agent"] = agent
