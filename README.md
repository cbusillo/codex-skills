# Codex Skills

Reusable skills for Codex-style coding agents, including OpenAI Codex CLI,
Code, and Every Code.

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

Preferred patterns:

```text
.local/
*.local.*
```

Examples:

```text
.local/profile.md
.local/github-repo-workflow.md
.local/github-plan.md
```

When a skill needs local context, it should treat the local file as optional and
continue to work without it. Commit `*.example.md` files when a template would
help other users configure their own private overlay.

Avoid storing tokens or passwords even in ignored files. Prefer environment
variables, credential helpers, or secret managers, and document only the variable
names a skill expects.

## GitHub Automation Token

The GitHub workflow skill includes `github-repo-workflow/scripts/gh-with-env-token`,
a small wrapper around `gh` that reads an ignored `.env` file and exports a token
only for the command it runs.

Copy `.env.example` to `.env` and set one of:

- `GH_TOKEN`
- `GITHUB_TOKEN`
- `CODEX_GITHUB_TOKEN`

Then call:

```sh
github-repo-workflow/scripts/gh-with-env-token pr view
```

The `.env` file is ignored by git. Do not commit real tokens.

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
`launchplane-context/references/public-safety.md`.
