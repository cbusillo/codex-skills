# Codex Skills

Reusable skills for coding agents. Claude Code and OpenAI Codex are both
supported hosts, along with Codex-compatible hosts such as Codex Lab. Skills and
helpers are written to behave the same on each; a skill that only makes sense on
one host says so in its description.
Every Code is retired; retained traces, fixtures, and artifact readers describe
historical behavior rather than a supported execution path.

Each skill lives in its own directory under [`skills/`](skills) with a `SKILL.md`
file; `skills/` is the catalog that hosts load. Skills can include
supporting references, scripts, agents, assets, and examples when the workflow
benefits from more than a single instruction file.

## Install

Clone this repository somewhere durable. For a new personal Codex installation,
use the current user-skill discovery location:

```sh
git clone git@github.com:OWNER/codex-skills.git ~/Developer/codex-skills
mkdir -p ~/.agents
ln -s ~/Developer/codex-skills/skills ~/.agents/skills
```

Inspect an existing destination before changing it; do not replace an existing
directory or symlink automatically. Established installations may still resolve
the catalog through `~/.code/skills`, `$CODE_HOME/skills`, or
`$CODEX_HOME/skills`. Preserve working bindings. Retiring Every Code does not
require renaming those paths or moving their data. For Codex Lab or another host,
verify that host's current discovery rules before adding a new binding.

See [Codex skill discovery](https://learn.chatgpt.com/docs/build-skills) for
current Codex locations and plugin-owned alternatives.

### Claude Code

Claude Code reads personal skills from a flat `~/.claude/skills/<skill>/`
folder, which usually holds other content and cannot be replaced by a link to
the catalog. Link the repository itself instead, once:

```sh
mkdir -p ~/.claude/skills
ln -s ~/Developer/codex-skills ~/.claude/skills/shared
```

The repository root is a Claude Code plugin: `.claude-plugin/plugin.json`, the
`skills/` catalog, and `hooks/`. Claude Code loads it in place as a
skills-directory plugin, so every skill, including one added later, appears
after a restart as `shared:<skill>` (for example `shared:github`). The prefix
keeps catalog skills apart from the host's own skills and commands of the same
name. `claude plugin list` shows the binding as `shared@skills-dir`, and
`claude plugin details shared` lists the skills and hooks it found.

The same install rule applies: inspect an existing destination first and do
not replace a directory or link automatically.

On invocation Claude Code gives the model the skill's base directory and the
Markdown body only; frontmatter, including command-policy metadata, is never
shown. The plugin therefore ships a `PreToolUse` hook ([`hooks`](hooks)) that
reads the same `policy.command_policies` frontmatter at run time, through the
policy simulator, and blocks a matching shell command with the policy's message
and preferred replacement. It splits compound lines and looks behind what an
agent commonly puts in front of a tool: environment assignments, `command`,
`exec`, `time`, `nohup`, `env`, `uv run`, a directory before the tool name, and
one `bash -c '...'` wrapper. It is a guardrail for habits, not a security
boundary; `xargs` and `sudo` are not unwrapped. It adds about 0.15 s per shell
command, needs `uv` on
`PATH`, and lets the command run if it cannot read the event or the policies. A
policy change needs no regeneration step.

### Layout and private local state

Hosts bind to the catalog, not to the repository: a Codex-family `skills` path
resolves to `<checkout>/skills`. Private, ignored local state that helpers
resolve through `$CODE_HOME/skills/.local` (then `$CODEX_HOME`, then `~/.code`)
therefore lives at `<checkout>/skills/.local`. On a machine with no
Codex-family binding, such as one that installs only through Claude Code, the
helpers that read that state find it relative to themselves, so nothing needs
setting.

Configuration and caches outside the catalog (`local.env`, `state/`) use one
order everywhere: `$CODE_HOME`, then `$CODEX_HOME`, then `~/.code`. Those are
only directory names and work on any host. One helper also reads an optional
`github-planning.json` from `~/.codex` when `~/.code` holds neither skills nor
plans, so that a stock Codex home keeps working. Shared references that several
skills link as `../references/...` live in `skills/references`. Repository
tooling (`scripts/`, `.github/`) stays at the root and is not part of an
install.

The repository's runtime reconciler checks `$CODE_HOME/skills`, then
`$CODEX_HOME/skills`, then `~/.code/skills`, then each entry under Claude Code's
`skills` folder (`$CLAUDE_CONFIG_DIR` or `~/.claude`). It acts on one that is a
worktree of the same clone as the merged worktree, preferring one already on
the default branch, and lists every binding it looked at in the receipt's
`bindings_checked`. A separate clone is not matched. It does not discover an
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
[`skills/github/references/execution-environment.md`](skills/github/references/execution-environment.md)
for the complete dependency-introduction and update policy.

## Reviews By Another Model

The `model-review` skill asks a model from another provider to review a change
read-only. It drives whichever of the OpenAI (`codex`), Anthropic (`claude`), and
Google (`agy`) CLIs are installed; none is required, and a missing one is
reported rather than blocking. Before relying on it, see what works on your
machine:

```bash
uv run skills/model-review/scripts/review_with_model.py check --repo .
```

`agy` is the one that needs setup. Run headless it stops at the first tool that
needs permission and returns an empty answer with exit code 0, which looks like
a reviewer that found nothing. The helper reports that as a failure and prints
the exact read-only allow rules to add to your own `agy` settings. They name
the repository being reviewed; point them at a directory that holds your
repositories to cover them all, and never at your home directory, because the
rule applies to every `agy` session. The helper changes that file only when you
run its `configure` subcommand, and refuses to start `agy` at all when your
settings already allow it to write files.

## Direction

The `direction` skill holds a repository's direction in one owner-approved
`DIRECTION.md` at the root, keeps milestones as waypoints that must be listed
there, and tells an executing agent to escalate a reviewer finding that would
delete, retire, or redirect work instead of judging it. Its read-only audit
reports drift:

```bash
uv run skills/direction/scripts/direction_audit.py --repo OWNER/REPO
```

Once a repository has `DIRECTION.md`, `gh-plan.py milestone-create` refuses a
title the file does not list, and `milestone-update` refuses a rename to one.

The plugin also ships a `SessionStart` hook, `hooks/direction_check_hook.py`.
It reads a local marker that `direction_mark.py` writes at the end of a daily
turn and that the audit script writes per repository when an audit completes,
and it opens a session with one reminder line while the turn is more than a
day old or the current repository's audit more than a week old. It never
reads stdin, always exits 0, runs only on `startup`, `resume`, and `clear`
(not after a compaction), and is bounded to 15 seconds with Python downloads
disabled. The marker is `~/.code/direction-last-check.json` on every host
unless `DIRECTION_MARKER` names another file. For Codex, register the same
script as a session-start command hook in its hooks configuration.

## Instruction scope

Execution skills share [task scope and authorization](skills/references/execution-scope.md).
Existing authorization is reused within its scope; exact-action approvals and
configured review, quality, delegation, and output requirements remain in force.
Detailed lifecycle and handoff procedures load only through the relevant skill's
reference links. Command-policy frontmatter remains in the owning entrypoint.
Install the shared top-level `skills/references/` directory with these skills; copying
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
`skills/plan/SKILL.md`.

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

The GitHub workflow skill includes `skills/github/scripts/gh-with-env-token`,
a small wrapper around `gh` that reads the user's ignored `local.env` file under
`$CODE_HOME`, `$CODEX_HOME`, or `~/.code` and exports a token only for the
command it runs.

Copy `.env.example` to `$CODE_HOME/local.env`, `$CODEX_HOME/local.env`, or
`~/.code/local.env`, matching the runtime home you use. Prefer a private GitHub
App installed only on the repositories the automation manages. Give it
`Contents: Read and write`, `Issues: Read and write`, `Pull requests: Read and
write`, and `Metadata: Read`, leave webhooks inactive, store its downloaded key
outside the repository with mode `600`, and set all three variables:

- `GITHUB_APP_ID`
- `GITHUB_APP_INSTALLATION_ID`
- `GITHUB_APP_PRIVATE_KEY_PATH`

Add permissions only for helpers you use: `Actions: Read` for CI diagnosis or
`Actions: Read and write` for workflow dispatch/rerun, `Checks: Read` and
`Commit statuses: Read` for complete PR check evidence, and `Secret scanning
alerts: Read` for the sanitized secret-scanning status reader. For GitHub
Enterprise, also set `GITHUB_APP_API_URL` to the REST API base; the wrapper
refuses to send an App JWT to `api.github.com` when `GH_HOST` names another host.

The wrapper mints an installation token when needed and caches it under the Code
home with owner-only permissions until shortly before expiry. Complete App
configuration takes precedence over user-token variables, so install the App on
every repository this automation must access. If App variables are absent, the
existing user-token configuration remains supported:

- `GH_TOKEN`
- `GITHUB_TOKEN`
- `CODEX_GITHUB_TOKEN`

Configure the automation role separately from the token:

- `CODEX_AUTOMATION_LOGIN`
- `CODEX_AUTOMATION_EMAIL`
- `CODEX_AUTOMATION_BOT_LOGINS` for an optional quoted, space-separated list
  of additional owner-controlled automation accounts used for bot
  classification, trusted managed-plan authorship, and trusted milestone
  creators in the direction audit; do not list third-party bots

When App authentication is enabled, set `CODEX_AUTOMATION_LOGIN` to the App's
bot login (normally the App slug followed by `[bot]`) so write preflight and the
identity probe verify the intended actor.

The `CODEX_` prefix on these, and the `CODE_HOME` and `CODEX_HOME` names, are
historical. They are stable names that mean the same thing on every host and are
not renamed.

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

Confirm the selected credential, current App installation, and acting GitHub
identity without performing a write:

```sh
github/scripts/gh-with-env-token --check
```

GitHub writes require a configured automation identity and token and never
change to the active local `gh` account implicitly. Set
`GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1` only for an explicitly approved
one-off command whose human-owned actor is acceptable.

The `local.env` file is local to the user account. Do not commit real
tokens.

### Standard Repository Rulesets

`skills/github/scripts/gh-rulesets.py` maintains two repository rulesets on the
default branch: one reserves updates for the repository owner and the configured
automation App, and the other requires code-owner review for `DIRECTION.md` and
`CODEOWNERS` without an App bypass. The helper clears automation-token variables
and verifies that the active `gh` account is the repository owner before reading
the full bypass configuration or writing anything. The configured App ID is
printed in every plan so the operator can verify the intended bypass actor.

Plan one or more repositories without changing GitHub:

```sh
uv run skills/github/scripts/gh-rulesets.py plan --repo OWNER/REPO
```

Apply requires an explicit acknowledgement of the owner-admin mutation. Applying
to more than one resolved repository also requires the exact count printed by a
fresh plan:

```sh
uv run skills/github/scripts/gh-rulesets.py apply \
  --repo OWNER/REPO \
  --confirm-owner-admin-write
```

Use `--all-owned --owner OWNER` for a complete non-archived inventory. Plan and
pilot first; do not use a broad apply as a discovery command. A repository where
the configured App is not installed will reject the App bypass actor; treat that
as a pilot finding, install or deliberately exclude the repository, and rerun
the idempotent plan before continuing. The direction audit reports
`ruleset_missing` when an adopted repository lacks either active standard
branch ruleset.

The direction rule intentionally has no bypass. An owner who is the sole code
owner cannot approve their own pull request, so direction changes should normally
arrive on an automation-authored branch for owner approval. An owner-authored
direction pull request requires a distinct eligible code owner to review it.

### Protected Workflow Review

Protected operator workflows should use
`skills/github/scripts/github_workflow_babysit.py`. The helper dispatches with the
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
`skills/launchplane/references/public-safety.md`.
