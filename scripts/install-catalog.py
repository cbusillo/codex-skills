#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["tomlkit==0.15.1"]
# ///
"""Install whole-catalog bindings, preserved global instructions and existing hooks."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
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


def catalog_skills_directory(path: Path) -> bool:
    resolved = path.resolve()
    return resolved.name == "skills" and (resolved.parent / "instructions" / "global.md").is_file() and (resolved.parent / "scripts" / "sync-global-instructions.py").is_file()


def personal_source(sync, destinations: list[Path], local: Path, *, new_destinations: set[Path] | None = None) -> str:
    """Import unmanaged text; generated documents require their known catalog prefix."""
    content = safe_file(local).strip()
    base = sync.render(ROOT / "instructions" / "global.md", ROOT / ".absent-personal-source")
    previous_base = safe_file(ROOT / ".local" / "catalog-global-source.md")
    if not local.exists() and (previous_base or (ROOT / ".local" / "catalog-install.json").exists()):
        raise ValueError(f"Private instruction source is missing: {local}; restore it, or create an empty file to intentionally remove private instructions")
    known = sync.render(ROOT / "instructions" / "global.md", local)
    initial_private = content
    texts = {path: safe_file(path) for path in destinations}
    bases = [base, previous_base]
    if not previous_base and any(text.startswith(sync.HEADER) and text != known for text in texts.values()):
        # On first adoption, identify the longest committed shared source before
        # interpreting the remainder as private text, including source trims.
        history = subprocess.run(["git", "--no-optional-locks", "-C", str(ROOT), "log", "-50", "--format=%H", "--", "instructions/global.md"], capture_output=True, text=True, timeout=30)
        for revision in history.stdout.splitlines() if history.returncode == 0 else []:
            old = subprocess.run(["git", "--no-optional-locks", "-C", str(ROOT), "show", f"{revision}:instructions/global.md"], capture_output=True, text=True, timeout=30)
            if old.returncode == 0:
                bases.append("\n\n".join(filter(None, (sync.HEADER, old.stdout.strip()))) + "\n")
    for path, text in texts.items():
        if not text or text == known:
            continue
        if text.startswith(sync.HEADER):
            matches = [prefix.rstrip() for prefix in sorted(set(bases), key=len, reverse=True) if prefix and (
                text.startswith(prefix.rstrip() + "\n") or text.strip() == prefix.strip()
            )]
            if matches and not local.exists() and not previous_base:
                raise ValueError(f"Ambiguous generated instructions: {path}; restore the authoritative private source in {local}, or create an empty file there if you have verified there are no private instructions, before rerunning")
            matched = matches[0] if matches else None
            if matched is None:
                raise ValueError(f"Generated instructions differ from known catalog source: {path}; reconcile personal text in {local}, preview scripts/sync-global-instructions.py, then write and rerun")
            if local.exists() and (not previous_base or path in (new_destinations or set())):
                if text[len(matched):].strip() != initial_private:
                    raise ValueError(f"Generated instructions differ from the private source: {path}; reconcile the host edits in {local}, preview scripts/sync-global-instructions.py, then write and rerun")
                continue
            continue  # The private source is authoritative after adoption.
        else:
            text = text.strip()
        if text and "\n\n" + text + "\n\n" not in "\n\n" + content + "\n\n":
            content = "\n\n".join(filter(None, (content, text)))
    return content + "\n" if content else ""


def install(home: Path, codex: Path, claude: Path, *, write: bool, updater: bool, show_diff: bool = False, refresh_instructions: bool = False) -> dict:
    if codex.resolve().is_relative_to(ROOT.resolve()) or claude.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Host configuration overlaps the catalog checkout; use separate host configuration directories and clone the catalog outside them as in README")
    sync = load_sync()
    if refresh_instructions and updater:
        raise ValueError("Instruction refresh does not configure an updater")
    installation_path = ROOT / ".local" / "catalog-install.json"
    previous_installation = json.loads(safe_file(installation_path) or "{}")
    if not isinstance(previous_installation, dict):
        raise ValueError("Invalid catalog installation receipt")
    links, pending = [], []
    if not refresh_instructions:
        codex_skills = home / ".agents" / "skills"
        # Keep an existing whole-catalog binding; otherwise share the discovery
        # directory with personal skills through Codex's recursive discovery.
        if not (codex_skills.is_symlink() and codex_skills.resolve() == (ROOT / "skills").resolve()):
            if codex_skills.resolve().is_relative_to(ROOT.resolve()):
                raise ValueError(f"Personal skills directory overlaps the catalog checkout: {codex_skills}; use a separate personal skills directory")
            if (codex_skills.exists() or codex_skills.is_symlink()) and (not codex_skills.is_dir() or catalog_skills_directory(codex_skills)):
                raise ValueError(f"Existing binding preserved: {codex_skills}; inspect it before rerunning")
            codex_skills /= "shared"
        if catalog_skills_directory(claude / "skills"):
            raise ValueError(f"Existing catalog folder preserved: {claude / 'skills'}; use a personal skills directory before adding the namespaced binding")
        links = [(codex_skills, ROOT / "skills"), (claude / "skills" / "shared", ROOT)]
        legacy = codex / "skills"
        if legacy.is_symlink() and catalog_skills_directory(legacy) and legacy.resolve() != (ROOT / "skills").resolve():
            raise ValueError(f"Existing binding preserved: {legacy}; inspect the other catalog and rebind deliberately before installing this checkout")
        if legacy.is_symlink() and legacy.resolve() == (ROOT / "skills").resolve():
            if codex_skills.name == "shared" and (codex_skills.exists() or codex_skills.is_symlink()):
                raise ValueError("Legacy and nested catalog bindings coexist; preserve the legacy binding and inspect the nested shared link before moving it aside and rerunning")
            links = links[1:]  # Preserve the existing discovery and system-skill names.
        elif codex_skills.name == "shared" and (ROOT / "skills" / ".system").exists():
            raise ValueError("Catalog system cache conflicts with namespaced discovery; inspect skills/.system and move the old cache outside the catalog before rerunning, or retain a legacy whole-catalog binding")
        if len({path.resolve() for path, _ in links}) != len(links):
            raise ValueError("Host skills bindings collide; use separate personal skills directories for Codex and Claude")
        if (claude / "skills").resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("Claude personal skills directory overlaps the catalog checkout; use a separate personal skills directory")
        pending = [(path, target) for path, target in links if binding(path, target)]
    destinations = [claude / "CLAUDE.md", codex / "AGENTS.md"]
    local = ROOT / ".local" / "global-instructions.md"
    personal = personal_source(sync, destinations, local, new_destinations={path for path in destinations if str(path) not in previous_installation.get("instruction_hashes", {})})
    shared_source = (ROOT / "instructions" / "global.md").read_text()
    base = "\n\n".join(filter(None, (sync.HEADER, shared_source.strip()))) + "\n"
    content = "\n\n".join(filter(None, (base.strip(), personal.strip()))) + "\n"
    requested = {"codex": str(codex), "claude": str(claude)}
    previous = {key: previous_installation.get(key) for key in requested}
    configuration_change = {"previous": previous, "requested": requested} if previous_installation and previous != requested else None
    unmanaged_sources = [str(path) for path in destinations if safe_file(path).strip() and not safe_file(path).startswith(sync.HEADER)]
    if previous_installation.get("home") and Path(previous_installation["home"]).resolve() != home.resolve():
        raise ValueError("Installed home differs; preserve this installation and use a separate catalog checkout for fixtures or another machine")
    if refresh_instructions and not previous_installation:
        raise ValueError("No installation receipt; run the installer once before refreshing instructions")
    hashes = previous_installation.get("instruction_hashes", {})
    if not isinstance(hashes, dict):
        raise ValueError("Invalid instruction hashes in catalog installation receipt")
    for path in destinations:
        text = safe_file(path)
        if str(path) in hashes and hashlib.sha256(text.encode()).hexdigest() != hashes[str(path)] and text != content:
            raise ValueError(f"Installed instructions changed: {path}; preserve the edits in {local}, preview scripts/sync-global-instructions.py, write the reconciled output and rerun")
    # All destinations are inspected before any mutation.
    instruction_preview = sync.synchronize(content, destinations, write=False)
    hook_outputs = {} if refresh_instructions else sync.prepare_codex_hooks(codex, catalog=ROOT)
    hook_preview = [entry for path, text in hook_outputs.items() for entry in sync.synchronize(text, [path], write=False)]
    launch_path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    launch_content = None
    launch_changed = False
    loaded = None
    domain = ""
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
            "Label": LABEL, "CodexSkillsInstaller": 1,
            "ProgramArguments": [uv, "run", "--quiet", "--no-python-downloads", str(ROOT / "scripts" / "catalog_runtime.py"), "--update"],
            "WorkingDirectory": str(ROOT), "StartInterval": 21600, "RunAtLoad": True,
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CODEX_HOME": str(codex), "CLAUDE_CONFIG_DIR": str(claude)},
            "StandardOutPath": str(ROOT / ".local" / "catalog-update.log"),
            "StandardErrorPath": str(ROOT / ".local" / "catalog-update-error.log"),
        }).decode()
        old = safe_file(launch_path)
        if old and old != launch_content:
            try:
                previous = plistlib.loads(old.encode())
            except (ValueError, plistlib.InvalidFileException):
                previous = {}
            if not (previous.get("CodexSkillsInstaller") == 1 and previous.get("Label") == LABEL and previous.get("WorkingDirectory") == str(ROOT)):
                raise ValueError(f"Existing launchd job preserved: {launch_path}; inspect and move it aside before rerunning")
        launch_changed = bool(old and old != launch_content)
        if write:
            domain = f"gui/{os.getuid()}"
            loaded = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True)
            if not loaded.returncode and not old:
                raise ValueError("Existing loaded launchd job preserved; inspect it with launchctl print and unload your old job before enabling this checkout's updater")
    if write:
        if not local.exists() or personal != safe_file(local):
            local.parent.mkdir(parents=True, exist_ok=True)
            sync.synchronize(personal, [local], write=True)
        for path, target in pending:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target, target_is_directory=True)
        outputs = sync.synchronize(content, destinations, write=True)
        sync.synchronize(base, [ROOT / ".local" / "catalog-global-source.md"], write=True)
        scheduled = updater or (previous_installation.get("scheduled_updater", False) and launch_path.is_file())
        scheduled_at = previous_installation.get("scheduled_at") if previous_installation.get("scheduled_updater") and launch_path.is_file() else None
        if scheduled and not scheduled_at:
            scheduled_at = dt.datetime.now(dt.timezone.utc).isoformat()
        installation = json.dumps({"home": str(home), "codex": str(codex), "claude": str(claude),
                                   "scheduled_updater": scheduled, "scheduled_at": scheduled_at,
                                   "updater_plist": str(launch_path) if scheduled else None,
                                   "shared_source_sha256": hashlib.sha256(shared_source.encode()).hexdigest(),
                                   "instruction_hashes": {str(path): hashlib.sha256(content.encode()).hexdigest() for path in destinations}}, indent=2) + "\n"
        sync.synchronize(installation, [installation_path], write=True)
        if hook_outputs:
            outputs += sync.write_codex_hooks(hook_outputs, codex, catalog=ROOT)
        if launch_content is not None:
            sync.synchronize(launch_content, [launch_path], write=True)
            (ROOT / ".local").mkdir(exist_ok=True)
            assert loaded is not None
            if not loaded.returncode and launch_changed:
                subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=True)
            if loaded.returncode or launch_changed:
                try:
                    subprocess.run(["launchctl", "bootstrap", domain, str(launch_path)], check=True)
                except subprocess.CalledProcessError as error:
                    raise ValueError("Catalog installed; scheduled updater activation failed. Rerun --write --updater from a logged-in GUI session") from error
    else:
        outputs = instruction_preview + hook_preview
    # Avoid printing private instruction text/diffs in the normal install output.
    return {"bindings": [{"path": str(path), "target": str(target), "state": "create" if (path, target) in pending else "current"} for path, target in links],
            "configuration_change": configuration_change, "unmanaged_instruction_sources": unmanaged_sources,
            "outputs": [{key: value for key, value in entry.items() if key != "diff" or show_diff} for entry in outputs],
            "private_source": str(local), "updater": "enabled" if updater and write else "requested" if updater else "unchanged" if previous_installation.get("scheduled_updater") else "off",
            "migrated_events": hook_outputs.migrated_events if hook_outputs else {},
            "hook_trust": sync.HOOK_TRUST_NOTICE}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Apply; default is a read-only preview")
    parser.add_argument("--show-diff", action="store_true", help="Include private instruction diffs in preview output")
    parser.add_argument("--updater", action="store_true", help="Also enable a guarded six-hour launchd updater")
    parser.add_argument("--refresh-instructions", action="store_true", help="Refresh installed global instructions only; leave bindings and hooks alone")
    parser.add_argument("--home-dir", type=Path, help="Fixture home in an isolated catalog checkout; overrides host environment directories")
    parser.add_argument("--codex-dir", type=Path, help="Explicit Codex destination")
    parser.add_argument("--claude-dir", type=Path, help="Explicit Claude Code destination")
    args = parser.parse_args()
    if args.home_dir and args.updater and args.write:
        parser.error("--home-dir is a fixture destination; use a read-only preview or mocked scheduler tests instead of activating a real launchd job")
    home = (args.home_dir or Path.home()).resolve()
    codex = home / ".codex" if args.home_dir else Path(os.environ.get("CODEX_HOME") or home / ".codex")
    claude = home / ".claude" if args.home_dir else Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    codex = (args.codex_dir or codex).resolve()
    claude = (args.claude_dir or claude).resolve()
    try:
        print(json.dumps(install(home, codex, claude, write=args.write, updater=args.updater, show_diff=args.show_diff, refresh_instructions=args.refresh_instructions), indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Catalog not installed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
