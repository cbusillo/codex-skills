---
name: plugin-creator
description: Create and scaffold plugin directories for Codex with a required `.codex-plugin/plugin.json`, optional plugin folders/files, and baseline placeholders you can edit before publishing or testing. Use when Codex needs to create a new local plugin, add optional plugin structure, or generate or update repo-root `.agents/plugins/marketplace.json` entries for plugin ordering and availability metadata.
metadata:
  short-description: Scaffold Codex plugins
resources:
  - path: scripts/create_basic_plugin.py
    kind: script
    description: Scaffold a Codex plugin directory and optionally update marketplace metadata.
  - path: references/plugin-json-spec.md
    kind: reference
    description: Canonical sample JSON for plugin manifests and marketplace entries.
commands:
  - name: create-basic-plugin
    source: skill
    resource_path: scripts/create_basic_plugin.py
    example_argv: ["uv", "run", "scripts/create_basic_plugin.py", "<plugin-name>"]
    purpose: Creates a plugin scaffold with required manifest placeholders.
  - name: create-basic-plugin-with-marketplace
    source: skill
    resource_path: scripts/create_basic_plugin.py
    example_argv: ["uv", "run", "scripts/create_basic_plugin.py", "<plugin-name>", "--with-marketplace"]
    purpose: Creates a plugin scaffold and adds or updates a marketplace entry.
---

# Plugin Creator

Resolve `<path-to-plugin-creator>` to the directory containing this `SKILL.md`.
Before editing a manifest or generating/editing marketplace entries, read
[the canonical JSON samples and field guide](references/plugin-json-spec.md).

## Scaffold

1. Choose the plugin destination. If it is not explicit, ask whether the user
   wants a repo-local or home-local plugin before generating marketplace entries.
2. Run the bundled scaffold helper. Names normalize to lowercase hyphen-case
   (spaces, underscores and punctuation become hyphens; repeated hyphens
   collapse) and must be at most 64 characters. The outer folder and manifest
   `name` must match the normalized name.

   ```bash
   uv run <path-to-plugin-creator>/scripts/create_basic_plugin.py <plugin-name>
   ```

   By default this creates `<repo-root>/plugins/<plugin-name>/` with the required
   `.codex-plugin/plugin.json`, full schema shape and complete `interface`.
   Keep this manifest present. Leave generated values as `[TODO: ...]`
   placeholders until a human or follow-up step explicitly fills them.
3. Add optional components and marketplace registration as needed:

   ```bash
   uv run <path-to-plugin-creator>/scripts/create_basic_plugin.py <plugin-name> \
     --path <parent-plugin-directory> \
     --with-skills --with-hooks --with-scripts --with-assets \
     --with-mcp --with-apps --with-marketplace
   ```

   `--path` names the parent, not the plugin folder. Component flags create
   `skills/`, `hooks/`, `scripts/`, `assets/`, `.mcp.json` and `.app.json`.
   Select only the components needed. For a home-local plugin, use:

   ```bash
   uv run <path-to-plugin-creator>/scripts/create_basic_plugin.py <plugin-name> \
     --path ~/plugins --marketplace-path ~/.agents/plugins/marketplace.json \
     --with-marketplace
   ```

   For an existing plugin, add `--register-only` to register it without writing
   any plugin files. To replace its marketplace entry (for example, changing
   `--auth-policy ON_USE`), use `--register-only --force`; this preserves its
   manifest and companion config. Component scaffold flags cannot be combined
   with registration-only mode.

   Use `--force` in scaffold mode only when intentionally overwriting files in an existing plugin
   path or replacing the marketplace entry for the same plugin name.

## Marketplace decisions

- Keep the marketplace at `<repo-root>/.agents/plugins/marketplace.json`; for a
  home-local plugin, treat home as the root. Keep `source.path` relative to that
  root; the helper derives the path from the actual destination, including custom
  `--path` parents. Defaults use the Git repository root even from a subdirectory,
  or the current directory outside Git. An explicit marketplace path follows
  the `<root>/.agents/plugins/marketplace.json` convention.
- Append entries: `plugins[]` order is Codex render order. Reorder only when
  explicitly requested.
- Preserve existing `interface.displayName`. This field belongs in the root
  `interface`, never individual plugin entries. For a new marketplace, seed
  top-level `name` and `interface.displayName` placeholders and a `plugins` array.
- Always include `policy.installation`, `policy.authentication` and `category`,
  even at defaults. New entries default to `AVAILABLE`, `ON_INSTALL` and
  `Productivity`. Override policy defaults only when the user explicitly specifies
  an allowed value; the canonical field guide lists them.
- Omit `policy.products` unless the user explicitly requests product gating.

## Validate

After editing this skill, run:

```bash
uv run <path-to-skill-creator>/scripts/quick_validate.py <path-to-plugin-creator>
```
