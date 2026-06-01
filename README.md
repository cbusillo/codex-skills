# Codex Skills

Reusable skills for Codex-style coding agents, including OpenAI Codex CLI and
Every Code.

Each skill lives in its own directory with a `SKILL.md` file. Skills can include
supporting references, scripts, agents, assets, and examples when the workflow
benefits from more than a single instruction file.

## Install

Clone this repository somewhere durable, then symlink it into the agent config
directory:

```sh
git clone git@github.com:OWNER/codex-skills.git ~/Developer/codex-skills
ln -s ~/Developer/codex-skills ~/.code/skills
```

If `~/.code/skills` already exists, move it aside before creating the symlink.

## Local Overrides

This repository is intended to be safe for public sharing. Put personal,
machine-specific, client-specific, or private workflow data in ignored local
files instead of committing it.

### System Skill Overrides

Every Code recreates bundled system skills under the runtime skills directory during
startup: `$CODE_HOME/skills/.system` for Every Code, with
`$CODEX_HOME/skills/.system` kept for compatibility. Treat `.system/` in this
repository as generated/vendor cache state, not as maintained source. Edit the
top-level skill directories instead.

Some top-level skills intentionally use the same names as bundled system skills
so they win by normal Every Code skill precedence:

- `openai-docs`
- `plan`
- `plugin-creator`
- `skill-creator`
- `skill-installer`

Keep that override allowlist explicit in the repo validator. Runtime `.system`
caches can differ by Every Code build, so validation fails only when an active
top-level skill overrides a bundled system skill that is not allowlisted. If Code
adds a new bundled system skill with the same name as a top-level skill, update
the top-level override skill or the validator allowlist intentionally instead of
editing `.system/` directly.

If an injected available-skills list points at a missing repo-local path such as
`.system/plan/SKILL.md`, treat that as stale runtime metadata. For allowlisted
overrides, the usable source path is the top-level override, for example
`plan/SKILL.md`.

Preferred patterns:

```text
.local/
*.local.*
```

Examples:

```text
.local/profile.md
.local/github.md
.local/launchplane.md
```

When a skill needs local context, it should treat the local file as optional and
continue to work without it. Commit `*.example.md` files when a template would
help other users configure their own private overlay.

Avoid storing tokens or passwords even in ignored files. Prefer environment
variables, credential helpers, or secret managers, and document only the variable
names a skill expects.

Keep local overrides out of skill instructions. Public `SKILL.md` files should
describe reusable behavior, while ignored local files hold machine-specific
defaults, account names, private repository routing, or temporary rollout notes.
If a local convention becomes broadly useful, promote only the public-safe
procedure and leave private values in the local overlay.

## GitHub Automation Token

The GitHub workflow skill includes `github/scripts/gh-with-env-token`,
a small wrapper around `gh` that reads the user's ignored `local.env` file under
`$CODE_HOME`, `$CODEX_HOME`, or `~/.code` and exports a token only for the
command it runs.

Copy `.env.example` to `$CODE_HOME/local.env`, `$CODEX_HOME/local.env`, or
`~/.code/local.env`, matching the runtime home you use, and set one of:

- `GH_TOKEN`
- `GITHUB_TOKEN`
- `CODEX_GITHUB_TOKEN`

Then call:

```sh
github/scripts/gh-with-env-token pr view
```

The `local.env` file is local to the user account. Do not commit real
tokens.

## Public-Safety Checklist

Before publishing or pushing a new skill, scan for:

- personal home paths
- private repository, organization, client, or project names
- tokens, keys, passwords, and copied command output containing secrets
- Launchplane-derived context such as internal hostnames, product/context names,
  private repo names, branch names, issue titles, work-request ids, provider
  details, and copied operational context
- generated local runtime files
- files under `.system/`, `.local/`, or `.disabled/`

One useful local scan:

```sh
rg -n --hidden --glob '!**/.git/**' \
  '(TOKEN|SECRET|PRIVATE|/Users/|github_pat_|ghp_|sk-[A-Za-z0-9])'
```

For Launchplane context-specific review, also see
`launchplane/references/public-safety.md`.
