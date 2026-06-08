---
name: just-every-plugins
description: Install, inspect, or use Just Every Codex plugins from the owned codex-skills marketplace, including Ultracode parallel workers and Auto Code Review hooks. Use when the user wants Codex plugin setup, Just Every plugins, Ultracode, auto-review hooks, Codex marketplace configuration, or to make OpenAI Codex use plugin capabilities normally associated with Every Code.
metadata:
  short-description: Install Just Every Codex plugins
resources:
  - path: scripts/install_just_every_plugins.py
    kind: script
    description: Installs or inspects the codex-skills Just Every plugin marketplace and plugins.
  - path: references/provenance.md
    kind: reference
    description: Source provenance and inspected upstream revisions for the Just Every plugin catalog.
commands:
  - name: install-just-every-plugins
    source: skill
    resource_path: scripts/install_just_every_plugins.py
    example_argv:
      ["uv", "run", "scripts/install_just_every_plugins.py", "install"]
    purpose: Adds the codex-skills Just Every marketplace and installs Ultracode and Auto Code Review.
  - name: check-just-every-plugin-status
    source: skill
    resource_path: scripts/install_just_every_plugins.py
    example_argv:
      ["uv", "run", "scripts/install_just_every_plugins.py", "status"]
    purpose: Reports Codex plugin marketplaces and installed plugins using the local Codex CLI.
---

# Just Every Plugins

Use this skill to make OpenAI Codex/Codex-style environments use the Just Every
plugin ecosystem from this `codex-skills` checkout.

This repository owns the marketplace configuration and workflow guidance. The
plugin implementations remain sourced from Just Every upstream repositories:

- `just-every/plugin-ultracode` for parallel Codex worker workflows.
- `just-every/plugin-auto-review` for hook-driven review of edited Codex turns.
- `just-every/plugins` as the upstream marketplace reference.

## Install Workflow

Prefer the helper script over handwritten plugin commands:

```bash
uv run <path-to-just-every-plugins>/scripts/install_just_every_plugins.py install
```

The helper adds this checkout as a local marketplace first:

```text
<codex-skills>
```

Codex discovers the marketplace manifest at
`<codex-skills>/.agents/plugins/marketplace.json`.

Then it installs:

- `ultracode@codex-skills-just-every`
- `auto-review@codex-skills-just-every`

Do not run `codex plugin marketplace upgrade codex-skills-just-every` for this
local marketplace; Codex only upgrades Git-backed marketplaces.

Use `--dry-run` to print commands without changing Codex plugin state:

```bash
uv run <path-to-just-every-plugins>/scripts/install_just_every_plugins.py install --dry-run
```

## Status Workflow

Check the current Codex plugin state with:

```bash
uv run <path-to-just-every-plugins>/scripts/install_just_every_plugins.py status
```

If the Codex CLI is unavailable or does not support plugin commands in the
current environment, use `install --dry-run` and report the exact commands the
user can run in a Codex environment that supports plugins.

## How To Use The Plugins

After installation, start a fresh Codex thread so plugin-provided skills and
hooks are discovered.

Use Ultracode when a task benefits from parallel investigation, competing plans,
independent review lanes, or a broader verification pass:

```text
Use Ultracode to investigate this UI architecture change from implementation,
accessibility, and regression-risk angles.
```

Use Auto Code Review when you want hook-driven review of edits before a turn
finishes. After installing it, open `/hooks` in Codex, trust the plugin hooks,
and verify they are enabled.

## Ownership Boundary

Do not vendor plugin implementation source into `codex-skills` unless license
and permission are explicit enough for that import. This skill owns:

- marketplace wiring
- install/status workflow
- provenance notes
- how agents should decide when to use the plugins

The plugin repositories own their implementation, tests, releases, and runtime
behavior. If a plugin bug or feature belongs in plugin code, file or implement
that work in the appropriate plugin repository instead of hiding a local fork in
this skills repo.

## Cleanup

Remove temporary clones and scratch install directories before closeout. Do not
commit plugin clones, generated `node_modules`, npm tarballs, or Codex runtime
plugin cache directories into `codex-skills`.
