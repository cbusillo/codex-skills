---
name: skill-installer
description: Install skills into Every Code or Codex-style agent skill directories from a curated list or a GitHub repo path. Use when a user asks to list installable skills, install a curated skill, or install a skill from another repo (including private repos).
metadata:
  short-description: Install curated skills from openai/skills or other repos
resources:
  - path: scripts/list-skills.py
    kind: script
    description: List installable skills from a GitHub repo path and annotate already-installed entries.
  - path: scripts/install-skill-from-github.py
    kind: script
    description: Install one or more skill directories from a GitHub repo path or tree URL.
  - path: scripts/github_utils.py
    kind: script
    description: Shared GitHub request helpers used by the installer scripts.
commands:
  - name: list-installable-skills
    source: skill
    resource_path: scripts/list-skills.py
    example_argv:
      ["uv", "run", "scripts/list-skills.py", "--path", "skills/.curated"]
    purpose: Lists available skills with installed annotations.
  - name: install-skill-from-github
    source: skill
    resource_path: scripts/install-skill-from-github.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/install-skill-from-github.py",
        "--repo",
        "openai/skills",
        "--path",
        "skills/.curated/<skill-name>",
      ]
    purpose: Installs a skill from a GitHub repository into the active skills directory.
---

# Skill Installer

Helps install skills. By default these are sourced from
https://github.com/openai/skills/tree/main/skills/.curated, but users can also
provide other locations. Experimental skills live in
https://github.com/openai/skills/tree/main/skills/.experimental and can be
installed the same way. The source repo is separate from the runtime destination:
Every Code installs into `$CODE_HOME/skills` when available, with Codex-compatible
fallbacks documented below.

Use the helper scripts based on the task:

- List skills when the user asks what is available, or if the user uses this skill without specifying what to do. Default listing is `.curated`, but you can pass `--path skills/.experimental` when they ask about experimental skills.
- Install from the curated list when the user provides a skill name.
- Install from another repo when the user provides a GitHub repo/path (including private repos).

Install skills with the helper scripts.

## Communication

When listing skills, output approximately as follows, depending on the context of the user's request. If they ask about experimental skills, list from `.experimental` instead of `.curated` and label the source accordingly:
"""
Skills from {repo}:

1. skill-1
2. skill-2 (already installed)
3. ...
   Which ones would you like installed?
   """

After installing a skill, tell the user to restart their agent harness to pick up
new skills. For Every Code users, say: "Restart Every Code to pick up new
skills."

## Scripts

All of these scripts use network, so when running in the sandbox, request escalation when running them.

- `scripts/list-skills.py` (prints skills list with installed annotations)
- `scripts/list-skills.py --format json`
- Example (experimental list): `scripts/list-skills.py --path skills/.experimental`
- `scripts/install-skill-from-github.py --repo <owner>/<repo> --path <path/to/skill> [<path/to/skill> ...]`
- `scripts/install-skill-from-github.py --url https://github.com/<owner>/<repo>/tree/<ref>/<path>`
- Example (experimental skill): `scripts/install-skill-from-github.py --repo openai/skills --path skills/.experimental/<skill-name>`

## Behavior and Options

- Defaults to direct download for public GitHub repos.
- If download fails with auth/permission errors, falls back to git sparse checkout.
- Aborts if the destination skill directory already exists.
- Installs into `$CODE_HOME/skills/<skill-name>` when `CODE_HOME` is set. When
  `CODE_HOME` is unset, the helper scripts use `$CODEX_HOME/skills` for
  compatibility, then prefer `~/.code/skills` if present, then fall back to
  `~/.codex/skills`.
- Multiple `--path` values install multiple skills in one run, each named from the path basename unless `--name` is supplied.
- Options: `--ref <ref>` (default `main`), `--dest <path>`, `--method auto|download|git`.

## Notes

- Curated listing is fetched from `https://github.com/openai/skills/tree/main/skills/.curated` via the GitHub API. If it is unavailable, explain the error and exit.
- Private GitHub repos can be accessed via existing git credentials or optional `GITHUB_TOKEN`/`GH_TOKEN` for download.
- Git fallback tries HTTPS first, then SSH.
- The skills at https://github.com/openai/skills/tree/main/skills/.system are preinstalled, so no need to help users install those. If they ask, just explain this. If they insist, you can download and overwrite.
- Installed annotations use the same `$CODE_HOME`, `$CODEX_HOME`, `~/.code`,
  then `~/.codex` lookup order as installs.
