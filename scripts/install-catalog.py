#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Install whole-catalog bindings, preserved global instructions and existing hooks."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.codex-skills.catalog-update"


def load_sync():
    spec = importlib.util.spec_from_file_location("sync_global", ROOT / "scripts" / "sync-global-instructions.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_file(path: Path) -> str:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Refusing symlink or non-file: {path}")
    return path.read_text() if path.exists() else ""


def binding(path: Path, target: Path) -> bool:
    if path.is_symlink() and path.resolve() == target.resolve():
        return False
    if path.exists() or path.is_symlink():
        raise ValueError(f"Existing binding preserved: {path}; move it aside yourself after inspecting it, then rerun")
    return True


def personal_source(sync, destinations: list[Path], local: Path) -> str:
    """Import unmanaged text; generated documents require their known catalog prefix."""
    content = safe_file(local).strip()
    base = sync.render(ROOT / "instructions" / "global.md", ROOT / ".absent-personal-source")
    previous_base = safe_file(ROOT / ".local" / "catalog-global-source.md")
    known = sync.render(ROOT / "instructions" / "global.md", local)
    for path in destinations:
        text = safe_file(path)
        if not text or text == known:
            continue
        if text.startswith(sync.HEADER):
            matched = next((prefix.rstrip() for prefix in (base, previous_base) if prefix and (
                text.startswith(prefix.rstrip() + "\n") or text.strip() == prefix.strip()
            )), None)
            if matched is None:
                raise ValueError(f"Generated instructions differ from current source: {path}; inspect and restore their private supplement in {local} before rerunning")
            text = text[len(matched):].strip()
        else:
            text = text.strip()
        if text and text not in content:
            content = "\n\n".join(filter(None, (content, text)))
    return content + "\n" if content else ""


def install(home: Path, codex: Path, claude: Path, *, write: bool, updater: bool) -> dict:
    sync = load_sync()
    links = [(home / ".agents" / "skills", ROOT / "skills"), (claude / "skills" / "shared", ROOT)]
    pending = [(path, target) for path, target in links if binding(path, target)]
    destinations = [claude / "CLAUDE.md", codex / "AGENTS.md"]
    local = ROOT / ".local" / "global-instructions.md"
    personal = personal_source(sync, destinations, local)
    content = "\n\n".join(filter(None, (sync.HEADER, (ROOT / "instructions" / "global.md").read_text().strip(), personal.strip()))) + "\n"
    # All destinations are inspected before any mutation.
    instruction_preview = sync.synchronize(content, destinations, write=False)
    config = tomllib.loads(safe_file(codex / "config.toml"))
    legacy_session = any("direction_check_hook.py" in str(group) for group in config.get("hooks", {}).get("SessionStart", []))
    hook_path = codex / "hooks.json"
    hooks = sync.render_codex_hook(hook_path, include_session_start=not legacy_session)
    hook_preview = sync.synchronize(hooks, [hook_path], write=False)
    launch_path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    launch_content = None
    if updater:
        if sys.platform != "darwin":
            raise ValueError("--updater supports macOS launchd; run catalog_runtime.py --update manually elsewhere")
        uv = shutil.which("uv")
        if not uv:
            raise ValueError("uv is required to schedule catalog updates")
        # Only schedule a checkout that can satisfy the clean-main update gate.
        sys.path.insert(0, str(ROOT))
        from scripts.catalog_runtime import checkout_state
        state = checkout_state(ROOT)
        if state["state"] == "blocked":
            raise ValueError(f"Cannot schedule this checkout: {state['reason']}")
        launch_content = plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": [uv, "run", "--quiet", "--no-python-downloads", str(ROOT / "scripts" / "catalog_runtime.py"), "--update"],
            "WorkingDirectory": str(ROOT), "StartInterval": 21600, "RunAtLoad": True,
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            "StandardOutPath": str(ROOT / ".local" / "catalog-update.log"),
            "StandardErrorPath": str(ROOT / ".local" / "catalog-update-error.log"),
        }).decode()
        old = safe_file(launch_path)
        if old and old != launch_content:
            raise ValueError(f"Existing launchd job preserved: {launch_path}; inspect and move it aside before rerunning")
    if write:
        if personal != safe_file(local):
            local.parent.mkdir(parents=True, exist_ok=True)
            sync.synchronize(personal, [local], write=True)
        base = sync.render(ROOT / "instructions" / "global.md", ROOT / ".absent-personal-source")
        sync.synchronize(base, [ROOT / ".local" / "catalog-global-source.md"], write=True)
        for path, target in pending:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target, target_is_directory=True)
        outputs = sync.synchronize(content, destinations, write=True)
        outputs += sync.synchronize(hooks, [hook_path], write=True)
        if launch_content is not None:
            sync.synchronize(launch_content, [launch_path], write=True)
            (ROOT / ".local").mkdir(exist_ok=True)
            domain = f"gui/{os.getuid()}"
            loaded = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True)
            if loaded.returncode:
                subprocess.run(["launchctl", "bootstrap", domain, str(launch_path)], check=True)
    else:
        outputs = instruction_preview + hook_preview
    # Avoid printing private instruction text/diffs in the normal install output.
    return {"bindings": [{"path": str(path), "target": str(target), "state": "create" if (path, target) in pending else "current"} for path, target in links],
            "outputs": [{key: value for key, value in entry.items() if key != "diff"} for entry in outputs],
            "private_source": str(local), "updater": "enabled" if updater and write else "requested" if updater else "off",
            "hook_trust": "unchanged; approve new entries through Codex /hooks once"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Apply; default is a read-only preview")
    parser.add_argument("--updater", action="store_true", help="Also enable a guarded six-hour launchd updater")
    parser.add_argument("--home-dir", type=Path, help="Fixture home; overrides host environment directories")
    args = parser.parse_args()
    home = (args.home_dir or Path.home()).resolve()
    codex = home / ".codex" if args.home_dir else Path(os.environ.get("CODEX_HOME") or home / ".codex")
    claude = home / ".claude" if args.home_dir else Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    try:
        print(json.dumps(install(home, codex, claude, write=args.write, updater=args.updater), indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Catalog not installed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
