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

Clone this repository somewhere durable, then install once on each machine:

```sh
git clone https://github.com/OWNER/codex-skills.git ~/Developer/codex-skills
cd ~/Developer/codex-skills
uv run scripts/install-catalog.py --write
```

The installer binds the entire catalog at `~/.agents/skills/shared` for Codex and
`~/.claude/skills/shared` for Claude Code, installs both hosts' global
instructions, and registers the existing Codex command-policy and session-start
hooks. It respects `CODEX_HOME` and `CLAUDE_CONFIG_DIR`. Approve newly registered
hooks through Codex's `/hooks` interface once; installing never grants trust.
Restart the harness to discover the bindings.

Run without `--write` to preview; add `--show-diff` to inspect instruction changes
locally (these may include private text). Existing personal instructions are preserved
in the ignored `.local/global-instructions.md` and included in both outputs;
changed instruction files are backed up. Existing unrelated bindings, symlink
instruction files, malformed settings, or generated instructions whose private
source cannot be identified are reported and left in place. Inspect the reported
path before moving it aside or restoring its private source, then rerun. Working
legacy catalog bindings and other host settings are preserved. Personal skills
in `~/.agents/skills` coexist with the nested catalog binding; an existing link
from that whole directory to this catalog remains in place.

The output names unmanaged instruction sources that will be combined. Review
`--show-diff` when those sources need tidying; the installer preserves their text
instead of choosing between personal rules. One checkout records one active
pair of host destinations. Reinstalling with different native host overrides
reconfigures that pair and reports the previous and requested directories.

Edit personal instructions in `.local/global-instructions.md`. The installer
records the generated host files and reports later hand edits instead of
overwriting them during a scheduled refresh. To adopt a hand edit, merge it
into that private source, preview `uv run scripts/sync-global-instructions.py`,
then run it with `--write` and rerun the installer. Run
`uv run scripts/catalog_runtime.py --update` to verify recovery and clear a
recorded failed update. Older manual-sync output
is adopted when it matches recent committed catalog history and that private source;
unrecognized output is preserved for inspection. A session hook pointing at
another checkout is also reported; inspect and update its path before rerunning.
If the private source is missing after installation, restore it before refreshing;
use an empty source file when you intend to remove its instructions.
When a missing private source leaves several historical interpretations of a
generated file, the installer preserves the file and asks you to restore that
source. It does not guess whether a formerly shared paragraph is now private.
Use an isolated catalog checkout for fixture homes: the installer stores the
private source and installation destinations in that checkout and refuses to
redirect an existing installation to a different home.
Keep host configuration directories outside the catalog checkout so installation
cannot overwrite repository instructions or dirty its source. Personal skills
directory symlinks are preserved and support the nested binding; links into
another catalog are reported instead of modifying that checkout. Use the
maintained catalog or inspect and rebind the destination before rerunning.

On macOS, opt into the guarded six-hour updater in the same install run:

```sh
uv run scripts/install-catalog.py --write --updater
```

It installs `~/Library/LaunchAgents/com.codex-skills.catalog-update.plist`.
The remote must be readable without interaction for unattended fetches; the
HTTPS clone above works for public catalogs. For private or SSH remotes, set up
non-interactive read access first or use manual updates.
If a job with the catalog label is loaded without an installer-owned plist,
the installer preserves it and reports the conflict. Inspect it with
`launchctl print gui/$(id -u)/com.codex-skills.catalog-update`; unload your old
job before enabling this updater. Fixture homes support previews and mocked
scheduler tests; `--home-dir --write --updater` cannot activate a real job.
The updater fetches and fast-forwards only a clean `main` with no local commits;
it never switches, resets, stashes, cleans, or merges divergence. A pull makes
new skills visible without adding links and refreshes installed global instructions
through its instruction-only refresh, preserving the private supplement and
leaving current bindings and hooks alone. The existing session-start hook prints
one catalog line for a stale or blocked checkout, a failed update, or a scheduled
check older than twelve hours. A manual pull that changes shared instructions
also reports a stale installation until the instruction refresh runs. Session
start performs no network calls. State
and logs live in the checkout's ignored `.local/` directory.
Run `uv run scripts/catalog_runtime.py --update` for a manual guarded update on
macOS or Linux. To stop scheduled updates, run
`launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.codex-skills.catalog-update.plist`,
then remove that specific plist; catalog bindings remain available.

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

The hook also unwraps `gh-with-env-token` and its authentication flags. A route
that explicitly prefers that wrapper stays allowed; a wrapped `gh pr merge`
still requires the `github` helper. Blocking messages name the skill to load.
Manual-only skills mirror Codex's `agents/openai.yaml` policy in Claude's
`disable-model-invocation` frontmatter, checked by the catalog validator.
Invoke those workflows with `/shared:skill-name` on Claude and `$shared:skill-name`
on Codex; Claude's field requires an actual user slash-command invocation.
The registered hook uses a JSON deny decision and an exit-zero launcher fallback,
so missing source or a uv startup failure cannot masquerade as a policy denial.

### Claude Cowork

Cowork in the Claude desktop app does not read `~/.claude/skills`; it installs
plugins from a marketplace. This repository is one:
`.claude-plugin/marketplace.json` lists the repository root as the `shared`
plugin. In the desktop app or on claude.ai:

1. Open **Customize**, then **Plugins**.
2. Select **Add**, then **Add marketplace**, and enter `OWNER/codex-skills`.
3. Install **shared** from that marketplace, then start a new Cowork task.

Turn on **Sync automatically** on the marketplace, or use **Check for
updates**, to pick up new commits. The plugin has no pinned version, so each
commit on the default branch is a new version. Cowork reads only the default
branch.

The install belongs to your claude.ai account, so Claude Code on the same
account also receives it as `shared@synced`. Where the repository is already
linked into `~/.claude/skills`, that link wins and the synced copy is reported
as not loaded; nothing changes for that machine. Do not also install the
marketplace plugin from the Claude Code command line on such a machine: an
installed marketplace plugin outranks the link and would replace the in-place
checkout with a cached copy.

### Shared global instructions and Codex hooks

[`instructions/global.md`](instructions/global.md) is the common source for
both hosts' global instructions. Use the installer in [Install](#install) for
initial adoption; it preserves private host instructions in the ignored
`.local/global-instructions.md`, which the renderer includes in both outputs.
For later instruction maintenance, edit that private source or land changes to
the shared source, then inspect the sync preview before writing:

```sh
uv run scripts/sync-global-instructions.py --codex-hook
uv run scripts/sync-global-instructions.py --codex-hook --write
```

If you are intentionally updating global instructions from a task worktree (where `.local` is missing) and want to overwrite the existing files, append `--allow-missing-local` to proceed without private instructions.

Run from the maintained runtime checkout after landing the source. The helper
generates `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, backs up changed files,
and refuses symlink destinations. `--home-dir` selects a fixture home for tests.
Native `CODEX_HOME` and `CLAUDE_CONFIG_DIR` overrides are respected; use
`--codex-dir` or `--claude-dir` for explicit host destinations. `CODE_HOME`
continues to locate shared catalog state, not either host's global instructions.
Omit `--codex-hook` to synchronize instructions alone.
If run from a task worktree where the local source is missing, the script refuses to overwrite existing files to prevent dropping private instructions. Use `--allow-missing-local` to override this and proceed.

Codex 0.157.0 supports a blocking `PreToolUse` hook, exposes shell calls as
`Bash` with `tool_input.command`, and honors exit 2 with a stderr reason.
It also accepts the JSON deny decision used by the registered launcher. Hooks
are enabled by default in that version; if the host explicitly disabled them,
restore its `features.hooks` setting before expecting enforcement.
`--codex-hook` renders the existing `hooks/hooks.json` PreToolUse declaration
into `~/.codex/hooks.json`, retaining other hooks. It does not grant hook trust;
review the new entry through Codex's `/hooks` interface. Once registered and
trusted, a catalog pull updates the same policy script on both hosts. The
Claude-only skills protocol is not added to Codex's base instructions.

See [routing evaluation](evals/README.md) for the matched before/after cases,
the host checks, and the distinction between command selection and live proof.

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
rule applies to every `agy` session. The helper changes that file only through
its `configure` and `repair` subcommands, and refuses to start `agy` at all
when your settings allow more than reading. When the only excess is a `find` or
`rg` command grant that an earlier version of the helper asked for, `repair`
backs the file up and removes just those grants; anything else, including
grants you added for your own sessions, it refuses to touch.

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
On Claude Code (`CLAUDECODE=1`) it first prints the shared
[skills protocol](skills/references/using-skills.md), including which skill owns
each intermediate step. This applies even outside direction repositories.
Codex already carries a skills protocol in its base instructions, so it does
not receive this additional copy.
In a repository with a root `DIRECTION.md`, it prints the shared
[executing loop](skills/references/executing-loop.md) at session start. The loop
defines `next`, `go`, escalation, landing, and closeout for either harness
when its session-start hook is registered.
It reads a local marker that `direction_mark.py` writes at the end of a daily
turn and that the audit script writes per repository when an audit completes,
and it also prints a reminder line while the turn is more than a
day old or the current repository's audit more than a week old. It never
reads stdin, always exits 0, runs only on `startup`, `resume`, and `clear`
(not after a compaction), and is bounded to 15 seconds with Python downloads
disabled. The marker is `~/.code/direction-last-check.json` on every host
unless `DIRECTION_MARKER` names another file. For Codex, register the same
script as a session-start command hook in its hooks configuration.
Claude's separate `compact` handler uses `--skills-only` to restore the protocol
without repeating the executing loop or overdue-audit reminder.

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

### Native helper build and CodeQL

The root `Package.swift` gives CodeQL's Swift autobuilder a target for
`skills/work-closeout/scripts/reminder_list.swift`. On macOS 14 or newer,
`swift build --product reminder-list` compiles that existing helper without
running it or accessing Reminders. Build output stays in ignored `.build/`.
The normal script invocation remains supported; the package is also the build
entry point used for Swift extraction. Existing CodeQL language scanning stays
enabled.

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
caches differ by host and build, so the repository gate does not read them. If a
host adds a new bundled system skill with the same name as a top-level skill,
update the top-level override skill or the validator allowlist intentionally
instead of editing `.system/` directly.

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
  classification, trusted managed-plan authorship, and trusted creators of open
  milestones in the direction audit; do not list third-party bots

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
