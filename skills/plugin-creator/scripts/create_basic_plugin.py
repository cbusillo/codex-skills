#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Scaffold a plugin directory and optionally update marketplace.json."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


MAX_PLUGIN_NAME_LENGTH = 64
DEFAULT_INSTALL_POLICY = "AVAILABLE"
DEFAULT_AUTH_POLICY = "ON_INSTALL"
DEFAULT_CATEGORY = "Productivity"
DEFAULT_MARKETPLACE_DISPLAY_NAME = "[TODO: Marketplace Display Name]"
VALID_INSTALL_POLICIES = {"NOT_AVAILABLE", "AVAILABLE", "INSTALLED_BY_DEFAULT"}
VALID_AUTH_POLICIES = {"ON_INSTALL", "ON_USE"}


def normalize_plugin_name(plugin_name: str) -> str:
    """Normalize a plugin name to lowercase hyphen-case."""
    normalized = plugin_name.strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    normalized = normalized.strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized


def validate_plugin_name(plugin_name: str) -> None:
    if not plugin_name:
        raise ValueError("Plugin name must include at least one letter or digit.")
    if len(plugin_name) > MAX_PLUGIN_NAME_LENGTH:
        raise ValueError(
            f"Plugin name '{plugin_name}' is too long ({len(plugin_name)} characters). "
            f"Maximum is {MAX_PLUGIN_NAME_LENGTH} characters."
        )


def build_plugin_json(plugin_name: str) -> dict:
    return {
        "name": plugin_name,
        "version": "[TODO: 1.2.0]",
        "description": "[TODO: Brief plugin description]",
        "author": {
            "name": "[TODO: Author Name]",
            "email": "[TODO: author@example.com]",
            "url": "[TODO: https://github.com/author]",
        },
        "homepage": "[TODO: https://docs.example.com/plugin]",
        "repository": "[TODO: https://github.com/author/plugin]",
        "license": "[TODO: MIT]",
        "keywords": ["[TODO: keyword1]", "[TODO: keyword2]"],
        "skills": "[TODO: ./skills/]",
        "hooks": "[TODO: ./hooks.json]",
        "mcpServers": "[TODO: ./.mcp.json]",
        "apps": "[TODO: ./.app.json]",
        "interface": {
            "displayName": "[TODO: Plugin Display Name]",
            "shortDescription": "[TODO: Short description for subtitle]",
            "longDescription": "[TODO: Long description for details page]",
            "developerName": "[TODO: OpenAI]",
            "category": "[TODO: Productivity]",
            "capabilities": ["[TODO: Interactive]", "[TODO: Write]"],
            "websiteURL": "[TODO: https://openai.com/]",
            "privacyPolicyURL": "[TODO: https://openai.com/policies/row-privacy-policy/]",
            "termsOfServiceURL": "[TODO: https://openai.com/policies/row-terms-of-use/]",
            "defaultPrompt": [
                "[TODO: Summarize my inbox and draft replies for me.]",
                "[TODO: Find open bugs and turn them into tickets.]",
                "[TODO: Review today's meetings and flag gaps.]",
            ],
            "brandColor": "[TODO: #3B82F6]",
            "composerIcon": "[TODO: ./assets/icon.png]",
            "logo": "[TODO: ./assets/logo.png]",
            "screenshots": [
                "[TODO: ./assets/screenshot1.png]",
                "[TODO: ./assets/screenshot2.png]",
                "[TODO: ./assets/screenshot3.png]",
            ],
        },
    }


def build_marketplace_entry(
    plugin_name: str,
    install_policy: str,
    auth_policy: str,
    category: str,
    source_path: str | None = None,
) -> dict[str, Any]:
    return {
        "name": plugin_name,
        "source": {
            "source": "local",
            "path": source_path or f"./plugins/{plugin_name}",
        },
        "policy": {
            "installation": install_policy,
            "authentication": auth_policy,
        },
        "category": category,
    }


def load_json(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return json.load(handle)


def build_default_marketplace() -> dict[str, Any]:
    return {
        "name": "[TODO: marketplace-name]",
        "interface": {
            "displayName": DEFAULT_MARKETPLACE_DISPLAY_NAME,
        },
        "plugins": [],
    }


def validate_marketplace_interface(payload: dict[str, Any]) -> None:
    interface = payload.get("interface")
    if interface is not None and not isinstance(interface, dict):
        raise ValueError("marketplace.json field 'interface' must be an object.")


def prepare_marketplace_json(
    marketplace_path: Path,
    plugin_name: str,
    install_policy: str,
    auth_policy: str,
    category: str,
    force: bool,
    plugin_root: Path | None = None,
) -> dict[str, Any]:
    if marketplace_path.exists():
        payload = load_json(marketplace_path)
    else:
        payload = build_default_marketplace()

    if not isinstance(payload, dict):
        raise ValueError(f"{marketplace_path} must contain a JSON object.")

    validate_marketplace_interface(payload)

    plugins = payload.setdefault("plugins", [])
    if not isinstance(plugins, list):
        raise ValueError(f"{marketplace_path} field 'plugins' must be an array.")

    source_path = None
    if plugin_root is not None:
        marketplace_root = marketplace_path.parent.parent.parent
        source_path = "./" + Path(os.path.relpath(plugin_root, marketplace_root)).as_posix()
    new_entry = build_marketplace_entry(plugin_name, install_policy, auth_policy, category, source_path)

    for index, entry in enumerate(plugins):
        if isinstance(entry, dict) and entry.get("name") == plugin_name:
            if not force:
                raise FileExistsError(
                    f"Marketplace entry '{plugin_name}' already exists in {marketplace_path}. "
                    "Use --force to overwrite that entry and, in scaffold mode, plugin files. "
                    "Use --register-only --force to update an existing plugin's entry without rewriting its files."
                )
            plugins[index] = new_entry
            break
    else:
        plugins.append(new_entry)

    return payload


def update_marketplace_json(
    marketplace_path: Path,
    plugin_name: str,
    install_policy: str,
    auth_policy: str,
    category: str,
    force: bool,
    plugin_root: Path | None = None,
) -> None:
    payload = prepare_marketplace_json(
        marketplace_path, plugin_name, install_policy, auth_policy, category, force, plugin_root
    )
    write_json(marketplace_path, payload, force=True)


def validate_write_destination(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists. Use --force to overwrite.")


def write_json(path: Path, data: dict, force: bool) -> None:
    validate_write_destination(path, force)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def create_stub_file(path: Path, payload: dict, force: bool) -> None:
    if path.exists() and not force:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a plugin skeleton with placeholder plugin.json."
    )
    parser.add_argument("plugin_name")
    parser.add_argument(
        "--path",
        default=None,
        help=(
            "Parent directory for plugin creation (defaults to <repo-root>/plugins, or <cwd>/plugins outside Git). "
            "When using a home-rooted marketplace, use <home>/plugins."
        ),
    )
    parser.add_argument("--with-skills", action="store_true", help="Create skills/ directory")
    parser.add_argument("--with-hooks", action="store_true", help="Create hooks/ directory")
    parser.add_argument("--with-scripts", action="store_true", help="Create scripts/ directory")
    parser.add_argument("--with-assets", action="store_true", help="Create assets/ directory")
    parser.add_argument("--with-mcp", action="store_true", help="Create .mcp.json placeholder")
    parser.add_argument("--with-apps", action="store_true", help="Create .app.json placeholder")
    parser.add_argument("--register-only", action="store_true", help="Register an existing plugin without writing any plugin files; --force replaces only its marketplace entry")
    parser.add_argument(
        "--with-marketplace",
        action="store_true",
        help=(
            "Create or update <repo-root>/.agents/plugins/marketplace.json, or <cwd> outside Git. "
            "Source paths point to the actual plugin destination relative to the marketplace root."
        ),
    )
    parser.add_argument(
        "--marketplace-path",
        default=None,
        help=(
            "Path to marketplace.json (defaults to <repo-root>/.agents/plugins/marketplace.json). "
            "For a home-rooted marketplace, use <home>/.agents/plugins/marketplace.json."
        ),
    )
    parser.add_argument(
        "--install-policy",
        default=DEFAULT_INSTALL_POLICY,
        choices=sorted(VALID_INSTALL_POLICIES),
        help="Marketplace policy.installation value",
    )
    parser.add_argument(
        "--auth-policy",
        default=DEFAULT_AUTH_POLICY,
        choices=sorted(VALID_AUTH_POLICIES),
        help="Marketplace policy.authentication value",
    )
    parser.add_argument(
        "--category",
        default=DEFAULT_CATEGORY,
        help="Marketplace category value",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw_plugin_name = args.plugin_name
    plugin_name = normalize_plugin_name(raw_plugin_name)
    if plugin_name != raw_plugin_name:
        print(f"Note: Normalized plugin name from '{raw_plugin_name}' to '{plugin_name}'.")
    validate_plugin_name(plugin_name)

    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        root = Path(result.stdout.strip()) if result.returncode == 0 else Path.cwd()
    except FileNotFoundError:
        root = Path.cwd()
    plugin_root = ((Path(args.path).expanduser().resolve() if args.path else root / "plugins") / plugin_name)
    marketplace_path = Path(args.marketplace_path).expanduser().resolve() if args.marketplace_path else root / ".agents/plugins/marketplace.json"
    if args.register_only:
        if any((args.with_skills, args.with_hooks, args.with_scripts, args.with_assets, args.with_mcp, args.with_apps)):
            raise ValueError("--register-only cannot be combined with scaffold component flags.")
        manifest = plugin_root / ".codex-plugin/plugin.json"
        if not manifest.is_file() or load_json(manifest).get("name") != plugin_name:
            raise ValueError("--register-only requires an existing plugin manifest with the matching name.")
        update_marketplace_json(marketplace_path, plugin_name, args.install_policy, args.auth_policy, args.category, args.force, plugin_root)
        print(f"Registered plugin: {plugin_root}")
        print(f"marketplace manifest: {marketplace_path}")
        return
    plugin_json_path = plugin_root / ".codex-plugin" / "plugin.json"
    validate_write_destination(plugin_json_path, args.force)

    marketplace_payload = None
    if args.with_marketplace:
        marketplace_payload = prepare_marketplace_json(
            marketplace_path, plugin_name, args.install_policy, args.auth_policy,
            args.category, args.force, plugin_root,
        )

    plugin_root.mkdir(parents=True, exist_ok=True)

    write_json(plugin_json_path, build_plugin_json(plugin_name), args.force)

    optional_directories = {
        "skills": args.with_skills,
        "hooks": args.with_hooks,
        "scripts": args.with_scripts,
        "assets": args.with_assets,
    }
    for folder, enabled in optional_directories.items():
        if enabled:
            (plugin_root / folder).mkdir(parents=True, exist_ok=True)

    if args.with_mcp:
        create_stub_file(
            plugin_root / ".mcp.json",
            {"mcpServers": {}},
            args.force,
        )

    if args.with_apps:
        create_stub_file(
            plugin_root / ".app.json",
            {
                "apps": {},
            },
            args.force,
        )

    if marketplace_payload is not None:
        write_json(marketplace_path, marketplace_payload, force=True)

    print(f"Created plugin scaffold: {plugin_root}")
    print(f"plugin manifest: {plugin_json_path}")
    if args.with_marketplace:
        print(f"marketplace manifest: {marketplace_path}")


if __name__ == "__main__":
    main()
