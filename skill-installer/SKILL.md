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

- Resolves the requested repository and ref through the GitHub API before fetching
  skill content. Mutable branches and tags are bound to one full commit SHA.
- Uses that resolved SHA for both direct archive download and git sparse checkout.
  Git fallback verifies the checked-out `HEAD` before installing anything.
- Bare ref names must identify exactly one branch or tag. If both exist, use an
  explicit ref such as `refs/heads/release` or `refs/tags/release`.
- Accepts full 40-character commit SHAs. Short SHAs are rejected because their
  identity is not stable enough for reproducible installs.
- Resolves `tree`/`blob` URL ref and path boundaries against GitHub. Ambiguous
  URLs fail closed; retry with explicit `--repo`, `--ref`, and `--path` values.
- Defaults to direct download for public GitHub repos after immutable resolution.
- If download fails with auth/permission errors, falls back to git sparse checkout
  at the same resolved SHA.
- Rejects symbolic links in installed skill trees and excludes git metadata so
  archive and git transports produce the same self-contained skill content.
- Aborts if the destination skill directory already exists.
- Installs into `$CODE_HOME/skills/<skill-name>` when `CODE_HOME` is set. When
  `CODE_HOME` is unset, the helper scripts use `$CODEX_HOME/skills` for
  compatibility, then prefer `~/.code/skills` if present, then fall back to
  `~/.codex/skills`.
- Multiple `--path` values install multiple skills in one run, each named from the path basename unless `--name` is supplied.
- Options: `--ref <branch|tag|refs/...|full-sha>` (default `main`),
  `--dest <path>`, `--method auto|download|git`.

Successful installs report public-safe provenance:

```text
Repository: openai/skills
Requested ref: main
Resolved SHA: 0123456789abcdef0123456789abcdef01234567
Installed:
- skills/.curated/example-skill -> ~/.code/skills/example-skill
```

## Notes

- Curated listing is fetched from `https://github.com/openai/skills/tree/main/skills/.curated` via the GitHub API. If it is unavailable, explain the error and exit.
- Private GitHub repos require `GITHUB_TOKEN` or `GH_TOKEN` for repository and
  ref resolution. Git fallback may then use existing HTTPS or SSH credentials
  to fetch the resolved commit.
- Git fallback tries HTTPS first, then SSH.
- The skills at https://github.com/openai/skills/tree/main/skills/.system are preinstalled, so no need to help users install those. If they ask, just explain this. If they insist, you can download and overwrite.
- Installed annotations use the same `$CODE_HOME`, `$CODEX_HOME`, `~/.code`,
  then `~/.codex` lookup order as installs.
