# Codex Skills

Reusable skills for OpenAI Codex and compatible hosts such as Codex Lab.
Every Code is retired; retained traces, fixtures, and artifact readers describe
historical behavior rather than a supported execution path.

Each skill lives in its own directory with a `SKILL.md` file. Skills can include
supporting references, scripts, agents, assets, and examples when the workflow
benefits from more than a single instruction file.

## Install

Clone this repository somewhere durable. For a new personal Codex installation,
use the current user-skill discovery location:

```sh
git clone git@github.com:OWNER/codex-skills.git ~/Developer/codex-skills
mkdir -p ~/.agents
ln -s ~/Developer/codex-skills ~/.agents/skills
```

Inspect an existing destination before changing it; do not replace an existing
directory or symlink automatically. Established installations may still resolve
the catalog through `~/.code/skills`, `$CODE_HOME/skills`, or
`$CODEX_HOME/skills`. Preserve working bindings. Retiring Every Code does not
require renaming those paths or moving their data. For Codex Lab or another host,
verify that host's current discovery rules before adding a new binding.

See [Codex skill discovery](https://learn.chatgpt.com/docs/build-skills) for
current Codex locations and plugin-owned alternatives.

The repository's runtime reconciler currently resolves `$CODE_HOME/skills`, then
`$CODEX_HOME/skills`, then `~/.code/skills`; it does not discover an
`~/.agents/skills`-only installation. For that installation, verify and refresh
the clean default-branch checkout through the normal GitHub post-merge checkout
workflow. A reconciler `not_applicable` result does not prove it is current.

Treat the checkout behind the active `skills` path as a runtime checkout: keep
it clean, on `main`, and current with `origin/main`. Use linked task worktrees
for skill development. After a skills PR lands, reconcile the runtime checkout
with the landed repo-local GitHub helper before relying on installed skill
behavior or provenance-sensitive evidence.

## Execution Environment

Repository validation uses uv `>=0.11.29,<1`, keeps Python 3.12 as its minimum
compatibility lane, continuously tests current stable Python 3.14, and pins
GitHub Actions jobs to the Ubuntu 24.04 runner major. Compatible uv/Python patch
releases and runner image revisions intentionally float within those bounds and
must keep passing the canonical gate. See
[`github/references/execution-environment.md`](github/references/execution-environment.md)
for the complete dependency-introduction and update policy.

## Instruction scope

Execution skills share [task scope and authorization](references/execution-scope.md).
Existing authorization is reused within its scope; exact-action approvals and
configured review, quality, delegation, and output requirements remain in force.
Detailed lifecycle and handoff procedures load only through the relevant skill's
reference links. Command-policy frontmatter remains in the owning entrypoint.
Install the shared top-level `references/` directory with these skills; copying
one skill folder alone does not preserve its cross-skill reference dependencies.

## Local Overrides

This repository is intended to be safe for public sharing. Put personal,
machine-specific, client-specific, or private workflow data in ignored local
files instead of committing it.

### System Skill Overrides

Hosts may expose bundled system skills or generate installation caches. Treat
`.system/` in this repository and installed plugin caches as generated/vendor
state, not as maintained source. Edit the top-level skill directories instead.
Cache locations and refresh behavior belong to the selected host; do not assume
the retired Every Code startup mechanism applies to Codex or Codex Lab.

Some top-level skills intentionally use the same names as bundled system skills
as deliberate user-maintained overrides. Verify the current host's selection
behavior; when both copies are exposed, select the maintained top-level source
by its full path rather than combine conflicting workflows:

- `openai-docs`
- `plan`
- `plugin-creator`
- `skill-creator`

Keep that override allowlist explicit in the repo validator. Runtime `.system`
caches can differ by host and build, so validation fails only when an active
top-level skill overrides a bundled system skill that is not allowlisted. If a host
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
.local/people.yaml
.local/people/<person-id>.md
```

Use repo-local `.local/people.yaml` only for project-specific people context or
overrides. Durable identity context for the person using the agent should live
in the `codex-skills` checkout's `.local/people.yaml` so it follows agents across repos.

When a skill needs local context, it should treat the local file as optional and
continue to work without it. Commit `*.example.md` files when a template would
help other users configure their own private overlay.

Use `.local/profile.md` as the maintained private profile overlay: durable
machine, account, workflow, and cross-repo preferences can live there when they
are not safe or useful to publish. Review and prune it during memory
distillation or rollout-friction closeout so stale local notes do not become
hidden instructions. If a profile note becomes generally reusable, promote only
the public-safe procedure into a skill or repo doc and keep private values in
the local overlay.

Avoid storing tokens or passwords even in ignored files. Contact details such as
email addresses, phone numbers, chat handles, and GitHub usernames may belong in
private local overlays such as `$CODE_HOME/skills/.local/people.yaml` or
repo-local `.local/people.yaml`, but credentials still belong in environment
variables, credential helpers, or secret managers. Public skills should document
only the variable names a workflow expects.

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

Configure the automation role separately from the token:

- `CODEX_AUTOMATION_LOGIN`
- `CODEX_AUTOMATION_EMAIL`
- `CODEX_AUTOMATION_BOT_LOGINS` for an optional quoted, space-separated list
  of additional automation accounts used only for bot classification

`GH_WITH_ENV_TOKEN_EXPECTED_LOGIN`, `GIT_COMMIT_AS_BOT_NAME`, and
`GIT_COMMIT_AS_BOT_EMAIL` remain supported as higher-precedence per-tool
overrides. The same local environment precedence is used by shell and Python
helpers: `CODEX_SKILLS_ENV_FILE`, `$CODE_HOME/local.env`,
`$CODEX_HOME/local.env`, then `~/.code/local.env`.

Existing installations that previously configured only a GitHub token must add
`CODEX_AUTOMATION_LOGIN` before GitHub writes will proceed. Quote values that
contain spaces, including multi-login lists.

Then call:

```sh
github/scripts/gh-with-env-token pr view
```

GitHub writes require a configured automation identity and token and never
change to the active local `gh` account implicitly. Set
`GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1` only for an explicitly approved
one-off command whose human-owned actor is acceptable.

The `local.env` file is local to the user account. Do not commit real
tokens.

### Protected Workflow Review

Protected operator workflows should use
`github/scripts/github_workflow_babysit.py`. The helper dispatches with the
configured automation token, captures GitHub's exact returned run ID, diagnoses
`waiting` runs through `pending_deployments`, and stops on a bounded timeout.
An environment approval requires an exact `--approve-environment` value and is
submitted with the active local `gh` account after automation-token variables
are cleared. Keep that active human account distinct from the automation actor;
the helper refuses a protected dispatch when the two identities are the same.

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

The standard validation gate includes a tracked-file secret scan:

```sh
uv run scripts/validate-public-safety.py
```

It checks tracked files only, so ignored local overlays stay private while
committed examples and docs are still scanned before PRs merge.

For Launchplane context-specific review, also see
`launchplane/references/public-safety.md`.
