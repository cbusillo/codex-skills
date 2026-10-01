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
An existing whole-catalog link in `CODEX_HOME/skills` is also kept without adding
a second binding. If both forms already coexist, inspect the reported nested
link before moving it aside. A leftover `skills/.system` cache without a legacy
binding is reported: inspect and move that cache outside the checkout before
using the namespaced binding, so system skills keep their own discovery names.

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
When the private source is missing and a generated file differs from the current
shared output, the installer preserves it and asks you to restore that source.
If inspection confirms there were no private instructions, create an empty
`.local/global-instructions.md` and rerun. It does not guess whether a formerly
shared paragraph is now private.
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
preserving bindings and unrelated hooks. Bound catalog hooks receive session-alert updates; removing all catalog hooks opts out of that refresh. The existing session-start hook prints
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
Invoke those workflows with `/shared:skill-name` on Claude. On Codex use the
name shown in its skill list: `$shared:skill-name` for the new catalog binding,
or `$skill-name` when a preserved legacy binding lists an unprefixed name.
Claude's field requires an actual user slash-command invocation.
The registered hook uses a JSON deny decision and an exit-zero launcher fallback,
so missing source or a uv startup failure cannot masquerade as a policy denial.

### Claude Cowork

Cowork in the Claude desktop app does not read `~/.claude/skills`; it installs
plugins from a marketplace. This repository is one:
`.claude-plugin/marketplace.json` lists the repository root as the `shared`
plugin. In the desktop app:

1. Switch the app to **Cowork** first. The **Customize** page belongs to the
   mode it was opened from; opened from **Code**, it installs into Claude Code
   instead and Cowork sees nothing.
2. Open **Customize**, then **Plugins**.
3. Select **Add**, then **Add marketplace**, and enter `OWNER/codex-skills`.
4. Install **shared** from that marketplace, then start a new Cowork task.

Turn on **Sync automatically** on the marketplace, or use **Check for
updates**, to pick up new commits. The plugin has no pinned version, so each
commit on the default branch is a new version. Cowork reads only the default
branch.

Cowork keeps its plugins apart from Claude Code's. Do not install the
marketplace plugin into Claude Code, from the Code mode's **Customize** page or
the command line, on a machine where the repository is linked into
`~/.claude/skills`: an installed plugin named `shared` outranks the link, even
while disabled, and Claude Code then reads a cached copy instead of the
checkout. To undo such an install, run
`claude plugin uninstall shared@codex-skills`.

#### What does not work in Cowork

A Cowork task runs in a Linux VM with `uv`, `git`, and `python3`, and loads the
skills from the synced plugin. Tested on 2026-10-01, these parts of the catalog
do not carry over:

- **No `gh`.** The VM has no GitHub CLI, so the `github`, `github-plan`,
  `babysit-pr`, and other GitHub helpers cannot run there.
- **No hook takes effect.** Command policies are not enforced, so a raw
  `gh pr merge` is not redirected to its helper, and the session-start skills
  reminder and executing-loop reference never appear. Both hooks fail silently
  by design, so this test cannot tell whether they never ran or ran and failed.

Run a skill's helper through the base directory Cowork shows when the skill
loads. `CLAUDE_PLUGIN_ROOT` is empty in the task's shell, as it is in Claude
Code's.

Cowork lists 24 of the 27 skills. The three missing ones, `memory-distillation`,
`plan`, and `rollout-friction`, are manual-only (`disable-model-invocation:
true`), which keeps them out of the model's skill list on Claude Code as well.
Invoking them by name in Cowork has not been tested.

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
`--codex-hook` renders the `hooks/hooks.json` command-policy and session-alert declarations
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

### Session alerts

The same catalog hook source registers advisory `Stop` alerts on both harnesses
and `Interrupt` alerts on Codex. Claude Code has no native Interrupt event, and
its Stop event does not run for user interruptions or API errors. These notices
are prompts for a supervisor to check session state, not completion records:
a Stop hook can run again when another hook continues the turn. The hook never
returns a continuation, approval, or blocking decision.

Each invocation appends one JSON line to `$CODE_HOME/session-events.jsonl`,
falling back to `~/.code/session-events.jsonl` on either harness. Set `CODE_HOME`
to the same shared directory for both harnesses when using an override.
Records contain `schema_version`, `harness`, `session_id`, optional `turn_id`
(null on Claude), UTC `time`, `event`, `advisory: true`, `clean: null`, and
`stop_hook_active` when supplied for Stop. Neither event proves clean final
completion; the supervisor must verify current session state. Messages,
transcripts, credentials and working directories are not copied. Concurrent
local writes append whole lines; storage errors leave the session running and
emit a short diagnostic. Delivery is best effort, without exactly-once or
all-outcomes coverage.

The installer renders Codex alerts into its existing `hooks.json` path, leaving
trust bookkeeping unchanged. The catalog updater refreshes only alert entries
for installations that still have catalog-owned hook bindings. Claude's plugin
loads Stop from the catalog hook source. Existing installations can preview
`scripts/install-catalog.py` and rerun it with `--write`. Codex may skip changed
or new definitions until reviewed through its supported `/hooks` flow; no trust
is granted or copied by the installer. To suppress Codex alerts independently,
leave their entries untrusted in `/hooks` while retaining other approved hooks.
If an instruction refresh cannot safely update a hook destination, it leaves
that destination untouched, reports the skipped alert refresh in its output
and updater receipt, and still refreshes instructions. The next session-start
status line names the skipped alert refresh and its recovery command.
The stream appears on the first alert;
once it exists a local supervisor can use:

```bash
tail -f "${CODE_HOME:-$HOME/.code}/session-events.jsonl"
```

Verify session state after each notice. Do not interpret `clean: null` or a
quiet stream as evidence that a session succeeded, failed, or is still active.
The hook command uses a three-second timeout with Python downloads disabled.
See the [Codex hook contract](https://learn.chatgpt.com/docs/hooks) and
[Claude Code hook contract](https://code.claude.com/docs/en/hooks#stop).

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

For a new GitHub.com automation identity, use
[Guided separate automation identity](#guided-separate-automation-identity).
For an existing App or GitHub Enterprise configuration, copy `.env.example` to `$CODE_HOME/local.env`, `$CODEX_HOME/local.env`, or
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

### Guided Separate Automation Identity

A sole owner can adopt and audit without an App. Later direction changes need
a different PR author so the owner can approve them; the direction rule keeps
code-owner review with **no bypass**. Use a private GitHub App for that author.
Unlike a separate machine-user account with a fine-grained token, it needs no
second account or manually renewed user token. Existing machine-user token
configuration above remains supported.

Run this on the machine where your agents run, from the catalog checkout:

```sh
uv run skills/github/scripts/github_app_setup.py start --owner OWNER --name 'Repository automation'
```

For an organization-owned App, add `--organization`. The guided flow supports
GitHub.com; existing GitHub Enterprise settings use the manual configuration
above. Review the permission list in GitHub before creating the App: the
manifest is derived from the catalog's [full-operation profile](skills/github/references/github-permissions.md),
keeps webhooks inactive, and adds no organization permissions. Grants do not
authorize actions outside your task.

Setup checks the account type before opening the browser. Its success result
names the exact `local.env` written; use the same home selection in the agent
session if its environment differs from the setup terminal.

The **owner** does two browser steps: name/create the private App under the
intended account, then install it on that account with **Only select
repositories**, choosing the adopted repositories. Return to the terminal and
press Enter. No new human collaborator or repository seat is needed. The helper
receives GitHub's registration callback on loopback, stores the key privately
under `~/.config/codex-skills/github-app/`, verifies the installation/account
and permissions, and writes the App IDs, key path, bot login and commit email
to the same `local.env` location the wrappers resolve. There is no config file
to hand-edit and no key or token to paste into chat.

Existing identity conflicts are reported before the browser steps. An **All
repositories** installation produces a visible scope notice, including its
access to future repositories; change the installation settings if only the
adopted repositories were intended. The helper preserves the owner's existing
ability to choose all repositories.

If setup stops after registration, keep the private directory printed by the
helper (also reported as `session` in its JSON result), finish the installation in GitHub, then resume:

```sh
uv run skills/github/scripts/github_app_setup.py resume --session /private/setup-directory
```

If installation discovery is ambiguous, add `--installation-id ID` from the
installation's GitHub settings URL. Existing identity variables are preserved
unless you explicitly add `--replace-identity`; that writes a private
`local-env-before-*.env` backup inside the private setup directory and updates existing actor/commit overrides
together. To undo that replacement, restore the reviewed backup to `local.env`
with mode `600`, leaving the App registration/installations intact until you
decide whether to remove them. Keep the saved key directory while this identity
is in use.

Replacement reports the previous primary login but does not automatically trust
it as a bot. When it is an **owner-controlled automation account**, add
`--previous-bot OLD-BOT` to preserve historical managed-plan and milestone
authorship through the existing `CODEX_AUTOMATION_BOT_LOGINS` setting. Existing
trusted bots from `local.env` are retained; temporary shell exports are not saved
as permanent trust. The helper rejects the App owner's login. For an organization,
you must also exclude every personal owner's login. Never pass a personal owner
or a third-party bot; old human-authored requests stay protected.

If the callback failed or the browser cannot reach this machine's loopback
address, use the App's GitHub settings to download/generate a private key,
restrict that file to mode `600`, install the App, and import it without editing
configuration:

```sh
uv run skills/github/scripts/github_app_setup.py import --owner OWNER --app-id ID --slug APP-SLUG --key /private/downloaded-key.pem
```

After a successful import, keep the reported private session (it contains the
configured key). You may remove the redundant downloaded copy once you have
verified the new configuration.

If GitHub created the App under the wrong account, its key is still saved
privately. Correct or transfer the App ownership in GitHub before resuming;
the helper verifies the current registration owner as well as the installation
account before writing configuration.

Check the separate author before writing:

```sh
skills/github/scripts/gh-with-env-token --check
uv run skills/github/scripts/github-capabilities.py audit --repo OWNER/REPO
```

The reported actor must be your App's `APP-SLUG[bot]`, distinct from the owner's
login. Audit each selected adopted repository; permission declarations and a
successful identity check alone do not prove private repository access. On an
already adopted repository, the agent opens a normal direction PR using the
bot commit/push and PR helpers, the owner reviews and approves it as the eligible
code owner, then the authorized merge follows green CI. A self-approval or
unreviewed direction merge remains refused. Owner-only ruleset plan/apply stays
with the owner through the standard helper below; setup never applies rulesets,
changes `CODEOWNERS`, or approves/merges a PR.

The automated setup acceptance uses an isolated Git repository and a fake
GitHub endpoint to exercise registration, private credential discovery, bot
commits/PR authorship and distinct owner approval under the current no-bypass
ruleset payload. It does not claim a real App was registered or a real owner's
browser completed the flow. Primary references:
[manifest registration](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
and [App versus machine-user accounts](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/differences-between-github-apps-and-oauth-apps).

### Standard Repository Rulesets

`skills/github/scripts/gh-rulesets.py` maintains two repository rulesets on the
default branch: one reserves updates for the repository owner and the configured
automation App when configured, and the other requires code-owner review for `DIRECTION.md` and
`CODEOWNERS` without an App bypass. The helper clears automation-token variables
and verifies that the active `gh` account is the repository owner before reading
the full bypass configuration or writing anything. The configured App ID is
printed in every plan so the operator can verify the intended bypass actor.
A GitHub App is optional: with no App configured, the landing ruleset retains
only the administrator bypass, and the result names the `no_app_bypass` limit.
Only administrators can then update the default branch. Incomplete or invalid
App configuration still fails rather than silently removing its bypass. If an
existing landing ruleset already has an App bypass, an unconfigured shell also
refuses: restore that App configuration and rerun the plan. The standard landing
ruleset name stays the same in both modes so the audit can recognize it. If the
App has deliberately been retired, the owner removes that obsolete bypass in
GitHub's repository ruleset settings before rerunning plan; missing configuration
alone is not treated as authority to retire a bypass.

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
pilot first; do not use a broad apply as a discovery command. Multi-repository
apply is sequential: a later refusal can leave earlier repositories updated,
with completed-repository receipts in the error. A fresh plan across the full
set catches predictable refusals, including an existing App bypass without
configuration, before any write. A repository where
the configured App is not installed will reject the App bypass actor; treat that
as a pilot finding, install or deliberately exclude the repository, and rerun
the idempotent plan before continuing. The direction audit reports
`ruleset_missing` when an adopted repository lacks either active standard
branch ruleset. When the owner explicitly selects their own reader with
`--gh gh` (or declares their own login with `--automation`), the audit reports
`owner_acts_as_automation` in `limits` and treats that login's milestone admissions
as owner decisions. This known attribution limit does not make coverage incomplete
or hide other findings; `ok` and `counts` still describe the findings. A reader
returning the owner instead of a separately configured automation login still
reports incomplete identity coverage.
With only the owner's own `gh` login, explicitly select it for the read-only audit:

```sh
uv run skills/direction/scripts/direction_audit.py --repo OWNER/REPO --gh gh
```

The default audit reader remains the automation wrapper; this explicit read-only
selection does not enable fallback for other helpers or authorize any write.

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
