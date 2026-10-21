# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import os
from pathlib import Path
import sys

import github_plan_closure


def native_helpers():
    directory = str(Path(__file__).resolve().parents[2] / "supervisor/scripts")
    if directory not in sys.path:
        sys.path.append(directory)
    import account_choice
    import session_record
    return account_choice, session_record


def configured_homes(*, strict: bool = False) -> list[Path]:
    """Canonical account authority, independent of caller configuration."""
    homes = []
    try:
        config = native_helpers()[0].load_config(
            path=github_plan_closure.closed_log().with_name("supervisor.toml"))
        homes.extend(Path(a["env"]["CODEX_HOME"]) for a in config["accounts"] if a["provider"] == "openai")
    except (ValueError, OSError, ImportError):
        if strict:
            raise
        pass  # Missing optional account configuration never establishes absence.
    resolved = set(p.expanduser().resolve() for p in homes)
    if strict and not resolved:
        raise ValueError("configured Codex homes are unavailable")
    return sorted(resolved)


def retained_homes(*, sources=(), actions=(), additional_homes=()) -> set[Path]:
    """Required retained observations; these do not confer configured authority."""
    homes = set(additional_homes)
    for action in actions:
        homes.update(github_plan_closure.action_homes(action)["known"])
    for source in sources:
        if not isinstance(source, dict) or source.get("harness") != "codex":
            continue
        for identity in (source, source.get("resumed_generation")):
            if isinstance(identity, dict) and isinstance(identity.get("codex_home"), str):
                try:
                    candidate = Path(identity["codex_home"]).expanduser()
                except RuntimeError as error:
                    raise ValueError("retained source home cannot be resolved") from error
                if candidate.is_absolute():
                    homes.add(candidate)
    return {Path(home).expanduser().resolve() for home in homes}


def observation_homes(*, strict=False, sources=(), actions=(), additional_homes=()) -> list[Path]:
    """One read scope; caller homes can add fences, never hide account homes."""
    homes = {*configured_homes(strict=strict), Path.home() / ".codex",
             Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
             *retained_homes(sources=sources, actions=actions, additional_homes=additional_homes)}
    return sorted({home.expanduser().resolve() for home in homes})


def process_liveness():
    native_helpers()
    import session_liveness
    return session_liveness
