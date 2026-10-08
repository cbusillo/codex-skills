# Codex Skills

Reusable skills for coding agents. Claude Code and OpenAI Codex are both
supported hosts. Skills and helpers are written to behave the same on each;
a skill that only makes sense on one host says so in its description.

For this repository's purpose, priorities, stop boundaries, and retirements,
read the Director's overall `DIRECTION.md` in the repository owner's
`OWNER/direction` repository, then [DIRECTION.md](DIRECTION.md).
[AGENTS.md](AGENTS.md) holds execution details for both hosts and is the only
repository agent-instruction filename; path-specific instructions use nested
`AGENTS.md` files. Generated host-global instructions remain host-specific.
Every Code traces, fixtures, and artifact readers describe historical behavior.
Remaining Codex Lab bindings await the separate
[support decision](https://github.com/cbusillo/direction/issues/21); this docs
audit does not remove them.

Each skill lives in its own directory under [`skills/`](skills) with a `SKILL.md`
file; `skills/` is the catalog that hosts load. Skills can include
supporting references, scripts, agents, assets, and examples when the workflow
benefits from more than a single instruction file.

## Install

Clone this repository somewhere durable, then install once on each machine:

```sh
git clone https://github.com/OWNER/codex-skills.git ~/Developer/codex-skills
cd ~/Developer/codex-skills
uv run scripts/install-catalog.py
uv run scripts/install-catalog.py --write
```

The installer binds the entire catalog at `~/.agents/skills/shared` for Codex and
`~/.claude/skills/shared` for Claude Code, installs both hosts' global
instructions, and registers the Codex command-policy, session-start, and
Stop/Interrupt alert hooks. It respects `CODEX_HOME` and `CLAUDE_CONFIG_DIR`. Approve newly registered
hooks through Codex's `/hooks` interface once; installing never grants trust.
Existing inline hooks are migrated to JSON and may stop running until reviewed
again through `/hooks`, including policies that previously blocked commands.
Restart the harness to discover the bindings.

Run without `--write` to preview; add `--show-diff` to inspect instruction changes
locally (these may include private text). Existing personal instructions are preserved
in the ignored `.local/global-instructions.md` and included in both outputs;
changed instruction files are backed up. Existing symlinked instruction files
are read for initial adoption and reported as current, preserved, or skipped; both the link and
its target stay unchanged, including a target that is the other selected instruction
file. Later linked-source edits stay in their external source and do not stop installation.
To propagate those edits into regular outputs, update `.local/global-instructions.md`
and rerun the installer; linked files remain under separate management.
Stale generated linked instructions are reported as skipped, including by scheduled
refresh. Preview with `--show-diff` to render the changes for the linked source's
external manager, including when both destinations share one target. Generated
linked text is preserved without guessing its private remainder; the catalog's
private source supplies regular outputs. The separate instruction sync command preserves links in the same way,
so it can reconcile regular outputs while another destination remains linked.
Dangling links and non-file targets are refused; restore the external source before retrying.
Existing unrelated bindings, malformed settings, or generated instructions whose private
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
preserving bindings and unrelated hooks. Bound catalog hooks receive session-alert
updates; removing all catalog hooks opts out of that refresh. The existing session-start hook prints
one catalog line for a stale or blocked checkout, a failed update, or a scheduled
check older than twelve hours. A manual pull that changes shared instructions
also reports a stale installation until the instruction refresh runs. Session
catch-up is described under [runtime binding lookup](#runtime-binding-lookup). State
and logs live in the checkout's ignored `.local/` directory.
Run `uv run scripts/catalog_runtime.py --update` for a manual guarded update on
macOS or Linux. To stop scheduled updates, run
`launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.codex-skills.catalog-update.plist`,
then remove that specific plist; catalog bindings remain available.

See [Codex skill discovery](https://learn.chatgpt.com/docs/build-skills) for
current Codex locations and plugin-owned alternatives.

### Walking a new person through setup

Two setup steps happen on screens only the person can use: creating the GitHub
App and its key in a browser, and trusting the catalog hooks in Codex. The agent
prepares each command and says what to expect; the person types it and reads
back what they see. Do them in this order, after `install-catalog.py --write`
has succeeded.

#### Create the automation App and its key

The person signs in to GitHub in their default browser, as the account that
will own the App, on the machine where the agents run. The agent fills in the
`start` command from [Guided Separate Automation Identity](#guided-separate-automation-identity):
`--owner` is that account's GitHub login, and `--name` should be unique, such as
the login followed by "automation", because App names are unique across GitHub.
The person runs it in their own Terminal window from the catalog checkout,
because the command waits for them to press Enter.

1. If it stops before opening a browser with `managed identity or host variables
   already exist`, or says the account type does not match, stop and bring that
   to the Director. Do not add `--replace-identity` without the Director.
2. The terminal prints `Private setup record:` with a folder, then `Open` with a
   local `http://127.0.0.1:` address, and the browser opens a short page with a
   **Create GitHub App** button. If no page opens, paste that address into the
   browser. Click the button.
3. GitHub shows its create-App page for the account, with the name and the
   permission list filled in. Check that the account is the intended one, change
   the name if GitHub says it is taken, and confirm. The browser returns to a
   local page reading "App key saved privately. Return to the terminal to
   install the App." Nothing is downloaded: GitHub hands the key straight to the
   helper, which keeps it in the `Private setup record:` folder.
4. The browser opens GitHub's install page; if it does not, open the address on
   the terminal's `Install on` line. Choose **Only select repositories**, pick
   the repositories being adopted, and click **Install**.
5. At `After installing in the browser, press Enter here:` press Enter. The
   terminal ends with `Configured APP-SLUG[bot] in` and the `local.env` path. A
   `Scope notice:` line means the App was installed on all repositories.
6. The agent runs the `--check` once and the capabilities audit for each
   repository chosen in step 4, from the same section. The reported actor must
   be `APP-SLUG[bot]`, not the person's own login.

If setup stops before step 5, for example `registration timed out; no
credentials were configured` after 15 minutes:

- If the browser showed "App key saved privately", the key is saved. Finish the
  installation and have the agent run `resume` from the same section with the
  `Private setup record:` folder as `--session`.
- If GitHub's **Settings → Developer settings → GitHub Apps** lists no App with
  that name, none was created. Run `start` again.
- Otherwise download the key by hand, as follows.

1. In that list, click **Edit** next to the App. Tell the agent the **App ID**
   near the top of the page and the slug, the last part of the page address.
2. Under **Private keys**, click **Generate a private key**. The browser
   downloads a file named like `APP-SLUG.YYYY-MM-DD.private-key.pem`, usually
   into Downloads. Tell the agent the file's name. Do not open it, paste it into
   a chat, or send it anywhere.
3. The agent restricts it with `chmod 600` and the file's path.
4. If the App is not installed yet, use **Install App** in the same settings and
   choose **Only select repositories** as in step 4 above.
5. The agent runs `import` from the same section with `--owner`, the App ID, the
   slug and the file's path. It prints a `Private setup record:` folder, which
   now holds the key in use; keep it. It ends with the same `Configured` line.
   After the `--check` and audit pass, delete the downloaded copy.

#### Trust the catalog hooks in Codex

Sessions started before the install do not see the catalog. The guiding session
can stay open; restart other Codex and Claude Code sessions when convenient.
Claude Code has no hook approval step.

1. The person opens a new Terminal window and runs `codex`.
2. Codex may open with **Hooks need review**, saying how many hooks are new or
   changed and that hooks can run outside the sandbox once trusted. Choose
   **Review hooks**. **Continue without trusting** or Esc leaves them off until
   you come back through `/hooks`. If no prompt appears, type `/hooks` and press
   Enter. The **Hooks** screen lists events with Installed and Active counts; a
   **Review** column and a warning that hooks need review mean some are waiting.
3. Use the arrow keys to select each event with a number under Review and press
   Enter. The catalog registers PreToolUse, SessionStart, Stop and Interrupt.
   Each hook shows its Event, Source, Command and Trust. A catalog hook's Command
   starts with `env CLAUDE_PLUGIN_ROOT=` followed by the catalog checkout path,
   and its Trust reads **New hook - review required** or **Modified since last
   trusted - review required**. Press `t` to trust it, then Esc to go back.
   Leave any hook you do not recognize alone. `t` on the event list trusts every
   waiting hook in every event, including ones that are not the catalog's.
4. The step is done when every catalog hook's Trust reads **Trusted**; hooks
   left alone may still show as waiting. Start a new Codex session for work.

### Claude Code

Claude Code reads personal skills from a flat `~/.claude/skills/<skill>/`
folder, which usually holds other content and cannot be replaced by a link to
the catalog. The installer above links the repository itself at
`~/.claude/skills/shared` instead; do not create that link again by hand.

The repository root is a Claude Code plugin: `.claude-plugin/plugin.json`, the
`skills/` catalog, and `hooks/`. Claude Code loads it in place as a
skills-directory plugin, so every skill, including one added later, appears
after a restart as `shared:<skill>` (for example `shared:github`). The prefix
keeps catalog skills apart from the host's own skills and commands of the same
name. `claude plugin list` shows the binding as `shared@skills-dir`, and
`claude plugin details shared` lists the skills and hooks it found.

The same install rule applies: inspect an existing destination first and do
not replace a directory or link automatically.

**Note:** The Cowork marketplace install was retired (#957). To remove an existing Cowork install: in Cowork, remove it under **Customize → Plugins**. If it was accidentally installed into Claude Code via the marketplace, remove it with `claude plugin uninstall shared@codex-skills` so it does not outrank the checkout link.


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

#### Auto mode

Claude Code reads `autoMode` from user or managed settings or a launch-time
`--settings` value, never from a repository's `.claude/settings.json`, so the
plugin cannot ship it. Without these entries, auto mode's classifier stops a
brief-authorized bot merge because no human approved it. The Director applies them by hand in
`/permissions` → Auto mode (or in their user `settings.json`) and checks the
result with `claude auto-mode critique`. Agents never edit the Director's
settings. Replace `OWNER` with the GitHub account and `CATALOG` with the
checkout path.

Add to `autoMode.environment`, replacing any older lines with the same labels.
Keep `"$defaults"` in each list; a list without it replaces the built-in
entries.

```json
"**Repository visibility**: github.com/OWNER/* repositories are a mix of public and private; a push to a public one is publishing",
"**Source control**: github.com/OWNER/* (the owner's own repositories) — all are trusted working repositories",
"**Trusted repos**: every github.com/OWNER/* repository; only a repository's own work belongs in its commits",
"**Org-specific CLIs**: the codex-skills helpers in CATALOG/skills/github/scripts (gh-pr.py, gh-issue, gh-comment, git-commit-as-bot, git-push-as-bot, gh-with-env-token) act as the configured automation account. Human PR approval is not a merge gate; green CI is the gate, and a change that calls for another model's review has it recorded before merging."
```

Set `autoMode.allow`, keeping the built-in rules:

```json
"allow": [
  "$defaults",
  "Merging a pull request in a github.com/OWNER/* repository with the codex-skills gh-pr.py merge helper (--method merge) after its CI is green, when the user's message in this session authorizes the agent to merge in that repository. Not covered: repositories where a merge deploys to production or that Launchplane's merge train lands, force pushes, and any deploy or promotion."
]
```

The exception does not cover production deploys or force pushes; the built-in
rules still judge those.

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

Run from the maintained runtime checkout after landing the source. The helper
generates `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, backs up changed files,
and preserves linked instruction files and their targets. Symlinked hook/config
destinations remain refused. `--home-dir` selects a fixture home for tests.
Native `CODEX_HOME` and `CLAUDE_CONFIG_DIR` overrides are respected; use
`--codex-dir` or `--claude-dir` for explicit host destinations. `CODE_HOME`
continues to locate shared catalog state, not either host's global instructions.
Omit `--codex-hook` to synchronize instructions alone.
If the local source is missing, restore it in the runtime checkout before
synchronizing. If you intend to remove all private instructions, use an empty
`.local/global-instructions.md` there, inspect the preview, then synchronize.
The helper also supports `--allow-missing-local`, which intentionally overwrites
generated instructions without the private source; it is not the task-worktree
maintenance path.

Codex 0.157.0 supports a blocking `PreToolUse` hook, exposes shell calls as
`Bash` with `tool_input.command`, and honors exit 2 with a stderr reason.
It also accepts the JSON deny decision used by the registered launcher. Hooks
are enabled by default in that version; if the host explicitly disabled them,
restore its `features.hooks` setting before expecting enforcement.
`--codex-hook` maintains the catalog's `PreToolUse`, `SessionStart`, `Stop`, and `Interrupt` hooks in
`~/.codex/hooks.json`, retaining other hooks. It also moves existing inline
event declarations from `config.toml` into JSON, preserving their commands,
matchers, timeouts, unrelated settings, and Codex-managed `[hooks.state]`.
An existing catalog direction hook is retained rather than registered twice.
Both the installer and sync helper use this same migration path. Unsupported
or conflicting definitions stop with an actionable error before writing.

For an existing installation, migrate hooks alone from the maintained runtime
checkout, reviewing the preview before applying it:

```sh
uv run scripts/sync-global-instructions.py --codex-hook --hooks-only
uv run scripts/sync-global-instructions.py --codex-hook --hooks-only --write
```

Full diffs are opt-in with `--show-diff` for a person's local review because TOML
context may contain private settings. Changed files receive private sibling backups. To undo migration, restore both
the `hooks.json` and `config.toml` backups reported by the helper; if JSON was
newly created, remove only that newly created file after restoring TOML.
Restore whole backups only if the files have not changed since migration.
Otherwise restore just the hook declarations while retaining newer settings
and Codex-managed trust state.
Repeating synchronization produces no changes once consolidated. Migrating a
hook changes its definition source, so review any untrusted entries through
Codex's `/hooks` interface before expecting them to run. The helper never grants,
copies, or invents hook trust. See the [official hook guidance](https://learn.chatgpt.com/docs/hooks).
Existing JSON entries can also require review when definitions or positions
change; the renderer retains existing policy positions when consolidating.
Standalone TOML comments attached to removed hook tables are retained at the
end of the remaining TOML. An interrupted migration reports completed-file
backups; preview again to reconcile identical declarations and finish migration.
Inline comments on migrated values remain recoverable in the TOML backup.
If `config.toml` is a symlink with inline hooks, install with
`--skip-codex-hooks` or use instruction-only sync without `--codex-hook` to keep
the existing hooks working. The installer reports skipped hook setup and leaves
its definitions and trust alone; a mixed-source warning may remain until migration.
To migrate, preserve the link
target's contents in a regular `config.toml`, then preview again.
Migration does not copy disabled choices from old source keys. The preview and
receipt identify recognized disabled handlers by event, their old TOML position,
and their destination JSON position. Keep those destination handlers disabled
when reviewing them through `/hooks`. A `deduplicated` entry has no destination:
its TOML copy was removed. Compare the existing JSON copy's independently
reviewed settings before changing it. If it came from an interrupted migration,
keep the original disabled choice when reviewing that copy. Other null
destinations mean the managed declaration replaced the source definition. Removing
a redundant managed `SessionStart` group can shift later JSON positions; review
those remaining handlers through `/hooks` too. Full commands
are available only in the person's local `--show-diff` review.
Once registered and
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

General configuration and caches outside the catalog (`local.env`, `state/`)
use this order: `$CODE_HOME`, then `$CODEX_HOME`, then `~/.code`. Those are
only directory names and work on any host. Planning configuration uses its own
runtime-home lookup in the
[GitHub plan config schema](skills/github/references/config-schema.md).
Shared references that several
skills link as `../references/...` live in `skills/references`. Repository
tooling (`scripts/`, `.github/`) stays at the root and is not part of an
install.

#### Runtime binding lookup

The repository's runtime reconciler checks `$CODE_HOME/skills`, then
`$CODEX_HOME/skills`, then `~/.code/skills`, then the preserved legacy
`~/.codex/skills`, then `~/.agents/skills` and
`~/.agents/skills/shared`, then each entry under Claude Code's
`skills` folder (`$CLAUDE_CONFIG_DIR` or `~/.claude`). It acts on one that is a
worktree of the same clone as the merged worktree, preferring one already on
the default branch, and lists every binding it looked at in the receipt's
`bindings_checked`. Both `~/.agents` layouts work for Codex-only installs, and
the cleanup helper protects these same bindings. The default legacy binding is
discovered even when `CODEX_HOME` is unset.
A separate clone is not matched. A reconciler
`not_applicable` result does not prove the runtime checkout is current.

Treat the checkout behind the active `skills` path as a runtime checkout: keep
it clean, on `main`, and current with `origin/main`. Use linked task worktrees
for skill development. After a skills PR lands, reconcile the runtime checkout
with the landed repo-local GitHub helper before relying on installed skill
behavior or provenance-sensitive evidence. A merge-train landing driven by
`launchplane-train-drive.py` does this itself. To catch up a clean install that
has fallen behind, run its own copy:
`uv run <runtime-checkout>/skills/github/scripts/reconcile-runtime-checkout.py --repo cbusillo/codex-skills`.

The registered session-start hook also runs this catch-up for its bound catalog,
so a landing without a local train driver is picked up on configured startup,
resume, or clear events on either harness. The maintained declarations skip
compaction; legacy matcher scope remains as configured. Manual unbound development
invocations skip catch-up. Discovery and network reads are bounded to five seconds, reporting a
blocker or failure without preventing the session from starting. Startup guidance
is flushed first. A local fast-forward and its verification retain the
reconciler's normal command bound, so the short read budget cannot interrupt them.
Claude plugin cache copies resolve the catalog through the runtime bindings above.
The reconciler's provenance check
still applies: if its source changed upstream, use the copy from a worktree at
the reported tip. The new copy must be reconciled once after this change lands
before the installed hook can provide automatic catch-up. Refresh an existing
Codex session hook from the reconciled runtime checkout by previewing
`uv run <runtime-checkout>/scripts/sync-global-instructions.py --codex-hook --hooks-only --upgrade-session-start`, then
adding `--write` to pick up the updated hook timeout; Claude reads it from
`hooks/hooks.json`. The updated command opts in with `--runtime-catchup`; old
registered commands keep printing guidance without starting a catch-up under
their shorter timeout. If Codex asks to trust the refreshed hook, use `/hooks`.
The explicit upgrade adopts plain legacy catalog invocations; custom commands
remain preserved, and omitting the option retains the installer's existing behavior.

## Execution Environment

Repository validation uses uv `>=0.11.29,<1`, keeps Python 3.12 as its minimum
compatibility lane, continuously tests current stable Python 3.14, and pins
GitHub Actions jobs to the Ubuntu 24.04 runner major. Compatible uv/Python patch
releases and runner image revisions intentionally float within those bounds and
must keep passing the canonical gate. See
[`skills/github/references/execution-environment.md`](skills/github/references/execution-environment.md)
for the complete dependency-introduction and update policy.

The validation gate imports the Codex manual helper offline before starting
parallel helper tests. This initializes Node's ESM loader and the helper's
builtins; printing `node --version` does not perform that initialization.
Cold initialization under disk contention reproduced the CLI's five-second
timeout locally. It is the leading explanation for the historical CI failures,
whose process traces were not retained. The regression keeps its five-second
deadline, and the import does not fetch the manual.

## Reviews By Another Model

The `model-review` skill asks a model from another provider to review a change
read-only. It drives whichever of the OpenAI (`codex`), Anthropic (`claude`), and
Google (`agy`) CLIs are installed; none is required, and a missing one is
reported rather than blocking. Before relying on it, see what works on your
machine:

```bash
uv run skills/model-review/scripts/review_with_model.py check --repo .
```

OpenAI and Anthropic reviews use Context Panel's account choice through the
[shared Supervisor reader](skills/supervisor/scripts/account_choice.py), with the
same private [account mappings](skills/supervisor/references/helpers.md). Each
review sets only its child's home, removes inherited provider auth/routing overrides,
and writes a count-only launch receipt before starting. Its JSON names the
chosen account and receipt. If the choice or receipt storage is unavailable,
the review fails before starting; it does not fall back to the caller's account.
Google keeps its existing account setup.

`agy` also needs tool permissions. Run headless it stops at the first tool that
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

For work in this repository, use the direction files linked above and the
[executing loop](skills/references/executing-loop.md). Issue-backed work is
claimed before its linked worktree is created. Launchplane's active merge-policy
record owns this repository's train enrollment:
use the [Launchplane skill](skills/launchplane/SKILL.md#merge-train-controller)
for authorized train entry and landing. Do not call `gh-pr.py merge` or merge
directly in GitHub. A task brief that assigns routing to a direction or
Supervisor session owns that handoff.
After a confirmed landing, reconcile the runtime checkout as described in
[AGENTS.md](AGENTS.md#runtime-checkout-discipline).

The `direction` skill holds a repository's direction in one Director-approved
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
In a repository without one, when its origin repository owner's
`OWNER/direction` repository is in the marker's audited repositories or is checked out as
`direction` beside the repository's main checkout, it prints that overall
direction's stop boundaries, read from the merged default branch (or from that
checkout's last-fetched default branch when GitHub cannot be read), with the
file's link and the executing loop. When neither can be read
it says so in one line. Repositories under other repository owners print
nothing extra.
It reads a local marker that `direction_mark.py` writes at the end of a daily
turn and that the audit script writes per repository when an audit completes,
and it also prints a reminder line while the turn is more than a
day old or the current repository's audit more than a week old. While this
machine's own turn is stale it reads the shared turn record in `OWNER/direction`
(see the `direction` skill), so a turn taken on another machine counts. It never
reads stdin, always exits 0, runs only on `startup`, `resume`, and `clear`
(not after a compaction), and is bounded to 15 seconds with Python downloads
disabled. The marker is `~/.code/direction-last-check.json` on every host
unless `DIRECTION_MARKER` names another file. For Codex, the installer or
`scripts/sync-global-instructions.py --codex-hook` registers this hook in
`hooks.json`; review it through `/hooks` once. Keep user-layer event definitions
in JSON so later setup does not recreate inline TOML declarations.
Claude's separate `compact` handler uses `--skills-only` to restore the protocol
without repeating the executing loop or overdue-audit reminder. Claude's
`UserPromptSubmit` handler uses `--turn` to print only the protocol's step table
with each prompt, about 230 tokens, because Claude Code otherwise shows the
protocol once while Codex re-sends its skill catalog; `evals/transcript-measure.py`
reports how real sessions on both harnesses load the owning skill.

### Session alerts

Inline dotfiles hook configurations can preview `--refresh-instructions --show-diff` and replace managed alert entries in place with the generated `catalog_alert_toml` in their authoritative `config.toml` source. Remove obsolete duplicate alert groups while retaining unrelated hooks, group positions, and native trust. Refresh recognizes current inline alerts without writing that source. Regular configurations can use the hook-only migration described above.

The same catalog hook source registers advisory `Stop` alerts on both harnesses
and `Interrupt` alerts on Codex (verified against 0.159.2). Claude Code has no native Interrupt event, and
its Stop event does not run for user interruptions or API errors. These notices
are prompts for a supervisor to check session state, not completion records:
a Stop hook can run again when another hook continues the turn. The hook never
returns a continuation, approval, or blocking decision.

Each invocation appends one JSON line to `$CODE_HOME/session-events.jsonl`,
falling back to `~/.code/session-events.jsonl` on either harness. Set `CODE_HOME`
to the same shared directory for both harnesses when using an override.
Records contain `schema_version`, `harness`, `session_id`, optional `turn_id`
(null on Claude), UTC `time`, `event`, `advisory: true`, `clean: null`, and
`stop_hook_active` (null unless a boolean is supplied for Stop). Neither event proves clean final
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
disable their reviewed definitions with the `/hooks` toggle while retaining
other enabled hooks. Leaving entries untrusted can prompt for review again
on later launches.
On either harness, set `SESSION_ALERTS_DISABLED=1` in the hook environment to
suppress alerts independently without changing other hooks. For example, launch
Claude Code with `SESSION_ALERTS_DISABLED=1 claude` or Codex with
`SESSION_ALERTS_DISABLED=1 codex`. Removing the variable resumes alerts.
If an instruction refresh cannot safely update a hook destination, it leaves
that destination untouched, reports the skipped alert refresh in its output
and updater receipt, and still refreshes instructions. Concurrent edits observed
since the hook preview are preserved and reported as skipped. The next session-start
status line names the skipped alert refresh and its recovery command. Hook writes
require a regular hooks.json file. A readable symlink with no catalog labels
is recognized as unbound and left alone. A bound symlink whose alert entries already match the catalog is current and
needs no write. A stale bound symlink or malformed destination remains unverified
and produces a notice, while checkout/instruction updates continue. Catalog
staleness and instruction drift take priority over this advisory notice. Reconcile that destination
through the documented installer preview. For a stale dotfiles symlink, use
`scripts/install-catalog.py --refresh-instructions --show-diff`: its skipped
entry includes `catalog_alert_entries` with the generated Stop/Interrupt groups
for your dotfiles source; apply those groups there, preserving their positions
and unrelated hooks. Then run `scripts/catalog_runtime.py --update` to clear
the old skipped-refresh receipt. This preview contains no trust state. Dotfiles
shared across hosts with different catalog paths need per-host rendered entries.
Supervisors own local retention of the events file; truncating it discards old
notices and subsequent invocations append new ones. Readers should skip malformed
lines, which can result from interrupted or partial storage writes.
The stream appears on the first alert;
once it exists a local supervisor can use:

```bash
tail -f "${CODE_HOME:-$HOME/.code}/session-events.jsonl"
```

Verify session state after each notice. Do not interpret `clean: null` or a
quiet stream as evidence that a session succeeded, failed, or is still active.
The hook command uses a three-second timeout with Python downloads disabled,
and launch failures return success to the harness so they cannot request Stop
continuation. Disposable routing-eval sessions suppress these alerts.
See the [Codex hook contract](https://learn.chatgpt.com/docs/hooks) and
[Claude Code hook contract](https://code.claude.com/docs/en/hooks#stop).

## Instruction scope

Execution skills share [task scope and authorization](skills/references/execution-scope.md).
The [shared global instructions](instructions/global.md) tell agents on both
hosts to read repository `AGENTS.md` and relevant nested files explicitly when
the host has not already supplied them; repository guidance does not depend on
Claude Code discovering that filename automatically.
They name people and permissions with the shared [role words](skills/references/role-words.md):
Director, Client, and admin.
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
the retired Every Code startup mechanism applies to Codex or Claude Code.

Some top-level skills intentionally use the same names as bundled system skills
as deliberate user-maintained overrides. Verify the current host's selection
behavior; when both copies are exposed, select the maintained top-level source
by its full path rather than combine conflicting workflows:

- `openai-docs`
- `plugin-creator`
- `skill-creator`

Keep that override allowlist explicit in the repo validator. Runtime `.system`
caches differ by host and build, so the repository gate does not read them. If a
host adds a new bundled system skill with the same name as a top-level skill,
update the top-level override skill or the validator allowlist intentionally
instead of editing `.system/` directly.

If an injected available-skills list points at a repo-local path that does not
exist, treat that as stale runtime metadata. Check the selected host's binding
using [runtime binding lookup](#runtime-binding-lookup).

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
  of additional Director-controlled automation accounts used for bot
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
overrides. Shell and Python helpers load the same file: `CODEX_SKILLS_ENV_FILE`
when it is set, even if that file is missing, and otherwise the first of
`$CODE_HOME/local.env`, `$CODEX_HOME/local.env`, and `~/.code/local.env` that
exists. A home variable whose directory has no `local.env` does not hide the
next one.

`~/.code/local.env` is the one location every harness finds without setting a
variable; Claude Code sets neither `CODE_HOME` nor `CODEX_HOME`. On a host
without `~/.code`, keep the file under a home directory and set the same
`CODE_HOME` or `CODEX_HOME` for every harness, or set `CODEX_SKILLS_ENV_FILE` to
its absolute path for every harness.

Existing installations that previously configured only a GitHub token must add
`CODEX_AUTOMATION_LOGIN` before GitHub writes will proceed. Quote values that
contain spaces, including multi-login lists.

Then call:

```sh
skills/github/scripts/gh-with-env-token pr view
```

Confirm the selected credential, current App installation, and acting GitHub
identity without performing a write:

```sh
skills/github/scripts/gh-with-env-token --check
```

`--check` requires a configured App or user token. With no token it fails
explicitly, even when active-auth fallback is allowed; use `gh auth status`
to inspect active local authentication. It does not accept a `gh` command.

GitHub writes require a configured automation identity and token and never
change to the active local `gh` account implicitly. Set
`GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1` only for an explicitly approved
one-off command whose human-owned actor is acceptable.

The `local.env` file is local to the user account. Do not commit real
tokens.

### Guided Separate Automation Identity

A Director who is the sole code owner can adopt and audit without an App.
Later direction changes need a different PR author so the Director can approve
them; the direction rule keeps code-owner review with **no bypass**. Use a
private GitHub App for that author.
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

The **Director** does two browser steps: name/create the private App under the
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
adopted repositories were intended. The helper preserves the Director's existing
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
it as a bot. When it is a **Director-controlled automation account**, add
`--previous-bot OLD-BOT` to preserve historical managed-plan and milestone
authorship through the existing `CODEX_AUTOMATION_BOT_LOGINS` setting. Existing
trusted bots from `local.env` are retained; temporary shell exports are not saved
as permanent trust. The helper rejects the login of the account that owns the App. For an
organization, you must also exclude every organization owner's personal login.
Never pass an organization owner's personal login or a third-party bot; old
human-authored requests stay protected.

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
the helper verifies the account that currently owns the registration as well as
the installation account before writing configuration.

Check the separate author before writing:

```sh
skills/github/scripts/gh-with-env-token --check
uv run skills/github/scripts/github-capabilities.py audit --repo OWNER/REPO
```

The reported actor must be your App's `APP-SLUG[bot]`, distinct from the Director's
login. Audit each selected adopted repository; permission declarations and a
successful identity check alone do not prove private repository access. On an
already adopted repository, the agent opens a normal direction PR using the
bot commit/push and PR helpers, the Director reviews and approves it as the eligible
code owner, then the authorized merge follows green CI. A self-approval or
unreviewed direction merge remains refused. Owner-only ruleset plan/apply stays
with the Director through the standard helper below; setup never applies rulesets,
changes `CODEOWNERS`, or approves/merges a PR.

The automated setup acceptance uses an isolated Git repository and a fake
GitHub endpoint to exercise registration, private credential discovery, bot
commits/PR authorship and distinct Director approval under the current no-bypass
ruleset payload. It does not claim a real App was registered or a real Director's
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
printed in every plan so the Director can verify the intended bypass actor.
A GitHub App is optional: with no App configured, the landing ruleset retains
only the administrator bypass, and the result names the `no_app_bypass` limit.
Only administrators can then update the default branch. Incomplete or invalid
App configuration still fails rather than silently removing its bypass. If an
existing landing ruleset already has an App bypass, an unconfigured shell also
refuses: restore that App configuration and rerun the plan. The standard landing
ruleset name stays the same in both modes so the audit can recognize it. If the
App has deliberately been retired, the Director removes that obsolete bypass in
GitHub's repository ruleset settings before rerunning plan; missing configuration
alone is not treated as authority to retire a bypass.

Plan one or more repositories without changing GitHub:

```sh
uv run skills/github/scripts/gh-rulesets.py plan --repo OWNER/REPO
```

Apply requires an explicit acknowledgement of the admin mutation. Applying
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
branch ruleset. When the Director explicitly selects their own reader with
`--gh gh` (or declares their own login with `--automation`), the audit reports
`owner_acts_as_automation` in `limits` and treats that login's milestone admissions
as Director decisions. This known attribution limit does not make coverage incomplete
or hide other findings; `ok` and `counts` still describe the findings. A reader
returning the Director's login instead of a separately configured automation login still
reports incomplete identity coverage.
With only the Director's own `gh` login, explicitly select it for the read-only audit:

```sh
uv run skills/direction/scripts/direction_audit.py --repo OWNER/REPO --gh gh
```

The default audit reader remains the automation wrapper; this explicit read-only
selection does not enable fallback for other helpers or authorize any write.

The direction rule intentionally has no bypass. A Director who is the sole
code owner cannot approve their own pull request, so direction changes should
normally arrive on an automation-authored branch for Director approval. A Director-authored
direction pull request requires a distinct eligible code owner to review it.

### Protected Workflow Review

Protected admin workflows should use
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

## Chrome across Claude account homes

Opt in with the ignored `.local/chrome.toml` in the installed catalog checkout:

```toml
pinned_home = "~/path/to/extension-account-home"
homes = ["~/.claude", "~/path/to/another-account-home"]
```

List every Claude account home here; the installer also includes its selected
Claude destination and the pinned home. All homes must already exist and be
signed in by the Director. The pinned home must match the Chrome extension's
account. This config chooses the browser child process's home, not the session's
provider account; Context Panel continues to choose session accounts. A symlink
at the native `~/.claude` default home keeps native default login semantics;
other home symlinks are reported before installation.

Preview and apply through `scripts/install-catalog.py` as above. Chrome previews
invoke native `mcp get` health checks: existing entries start their child process
and may update Claude's own cache/state, even without `--write`. They do not add
or remove MCP entries. Avoid this preview on homes you must keep entirely
unchanged; use isolated fixture homes instead. The installer uses Claude's
[native MCP management commands](https://code.claude.com/docs/en/mcp#add-mcp-servers-from-json-configuration)
to install one user-scoped `catalog-chrome` server per home, invoking
`claude --claude-in-chrome-mcp` with the pinned `CLAUDE_CONFIG_DIR`. It never
uses the caller's authentication overrides: the native `env -u` launcher removes
those variable names without reading their values. For a pinned `~/.claude`
default account, it also unsets `CLAUDE_CONFIG_DIR`, preserving Claude's native
default login/config location rather than creating an alternate config there.
It never opens login files or a credential store. Unrelated configuration stays under
Claude's own management. An identical entry is kept; a conflicting or unreadable
home stops the explicit catalog install before its writes. To install bindings
and instructions while leaving Chrome enrollment unchanged, move aside
`.local/chrome.toml` and rerun; instruction-only refresh also skips Chrome.
The ignored `.local/chrome-install.json` records owned
entries, so changing only `pinned_home` updates those entries on the next explicit
install. A failed update attempts to restore the previous managed entry; a
failed restoration names the receipt to recover from. Inspect a reported
conflict with `claude mcp get catalog-chrome`, reconcile that entry deliberately,
and preview again. Removing a home from the list leaves its installed entry
in place; remove it explicitly with `claude mcp remove catalog-chrome -s user`
in that home if desired. Instruction-only scheduled refreshes preserve MCP
configuration; they do not enroll new accounts.

After installing, start fresh sessions without `--chrome`; the user entry
already supplies the browser tools. The Chrome server entry point is internal
to Claude Code and may change with a CLI update. Acceptance after installation
or a CLI update: from a signed-in session on an account different from the
extension, call only `mcp__catalog-chrome__list_connected_browsers` and verify
the existing Chrome appears. Do not pair, select, navigate or modify the browser
for this check. An installer success alone does not prove cross-account pairing.
