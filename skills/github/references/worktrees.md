# Task worktrees

Read before creating a task worktree or configuring a host's worktree root.
Claim issue-backed work with `github-plan` first. From any checkout, call the
catalog's helper by its absolute path; no PATH installation is needed:

```sh
<catalog>/skills/github/scripts/dev-worktree /absolute/repository task-slug origin/main
```

Here `<catalog>` is the checkout containing the loaded skills, not a literal
directory name. The command creates `work/task-slug` at the explicit starting
commit and prints the new path. Before creation it refreshes the primary
default-branch checkout using the [checkout refresh rules](post-merge-checkouts.md),
then runs the guarded
`landed_worktrees.py --all --apply` for that repository, reporting retired and
kept trees with reasons on stderr. Unavailable cleanup evidence keeps trees and
creation continues; it never weakens a retirement guard. A per-start
read budget stops new checks, while any guarded retirement already in progress
finishes without a timeout interrupting its lock restoration. Give creation
a command timeout of at least three minutes where the harness sets one.
Budgeted checks vary their order so held trees do not always consume the first
checks. It accepts a main checkout or a linked worktree;
both use the main repository's directory name. The default destination is
`~/Developer/worktrees/<repo>/task-slug`. Existing destinations (including empty
directories and dangling symlinks), branches and registered worktrees are never
reused or replaced. Choose another task slug on collision. Bare, submodule and
separate-git-dir layouts are unsupported.

The portable creator clears inherited `GIT_*` variables for its local Git
commands. Hook object/quarantine storage, discovery ceilings, and injected
configuration cannot override the explicit source or hide a destination inside
another repository. Normal repository, user, and system config files still
apply at their normal locations. Alternate files selected by `GIT_CONFIG_GLOBAL`
or `GIT_CONFIG_SYSTEM`, environment-only `safe.directory` grants, `GIT_EXEC_PATH`,
and Git tracing overrides are also ignored. Keep needed settings in the normal
`~/.gitconfig`, `$XDG_CONFIG_HOME/git/config`, system or repository config as
appropriate; put protected settings such as `safe.directory` in user/system
config. A relocatable Git installation must work from PATH without a caller
`GIT_EXEC_PATH`. Configured host executables retain their caller environment
and own their Git handling.

## Per-host configuration

The helper reads `~/.config/codex-skills/worktrees.json`. An absent default file
uses the portable default above. `DEV_WORKTREE_CONFIG=/absolute/file.json`
selects another file; an explicitly selected missing file or invalid settings
refuse rather than falling back. This is private host configuration, independent
of CLI account homes and repository settings. Neither creation nor the catalog
installer writes it.

```json
{
  "root": "/Users/me/Developer/worktrees"
}
```

Paths must be absolute; a leading `~` expands to the current user's home.
The portable helper keeps the resolved root outside repositories and refuses a
symlink at `<root>/<repo>`. It creates missing directories for an ordinary
local root. For external storage that requires mount identity checks, use the
host integration below so an absent mount cannot become an internal directory.
Follow any stricter host storage policy before using the helper.

## Existing host helper integration

A host with its own storage, lock and retirement policy can configure
`hostHelper` with an explicit `root`. The catalog command delegates creation arguments
to that executable, preserves its exit status and output, and creates no
directories itself. A failure never falls back to portable creation. The host
helper owns enforcement of the root: in this mode `root` is an informational
location for agents, not a second enforcement check. Set it to the location the
host helper actually implements and keep it aligned during storage migrations.
The Python layer replaces its process with the host executable, avoiding a
Python subprocess timeout that could kill cleanup during interrupted retirement.
The outer `uv` launcher remains its parent. The POSIX process-group interrupt
fixture exercises the shipped wrapper too; other signal and harness delivery
paths have not been qualified here.
A delegation marker refuses shims that call back into the catalog while the
original host process is alive, and refuses an identical inherited request even
if a shim detaches and exits. On POSIX, a surviving post-create child can start a
different request after the original host exits. A child that calls back before
the original host exits still refuses. Matching uses literal arguments:
detached shims that rewrite them are outside this recursion guarantee. Configure
`hostHelper` to the actual host executable rather than such a launcher.
Non-POSIX hosts retain
the conservative inherited-marker refusal; native Windows child-lifetime
behavior has not been qualified. Process ID reuse can also conservatively refuse
a later call; an unrelated child can remove `DEV_WORKTREE_DELEGATED` from its
own environment before explicitly starting an independent request.
This setting selects an existing trusted host executable; it does
not install, replace or edit one. Its `retire --dry-run` contract must be read-only;
creation bounds that preview using the existing process-group probe, while apply
finishes without an interrupt that could prevent lock restoration.
Explicit retirement first qualifies the exact target's source and ownership
through the common GitHub checks, then replaces the catalog process with the
host executable for its full preview or apply. Its signal cleanup and existing
`--offline-only` mode remain available. Preview changes no worktree.

Chris-Studio keeps its existing host-local helper and external-volume checks
with this configuration:

```json
{
  "root": "/Volumes/Developer-Artifacts/worktrees",
  "hostHelper": "/Users/cbusillo/.local/bin/dev-worktree"
}
```

Verify this per-host setting before creation; the catalog entry delegates to
the configured host helper. If it is absent, report the missing host configuration; the JSON above is
the supported configuration route. Do not use the portable default on Chris-Studio. The existing helper verifies the volume identity before creating anything,
locks registrations against unmounted-volume pruning. Use the catalog entry
above with this host configuration, so creation also
runs repository retirement. Keep the host helper unchanged; never make a
replacement volume directory or fall back to internal storage. A catalog pull
does not provision host storage or change runtime configuration.

## Retirement

Use `work-closeout` to establish the task's disposition and retained artifacts.
Retain a tree needed by ongoing work through a handoff under that work's
ownership. Once continuing use ends, release and verify its claim before
retiring the tree. Run from outside the target tree.
For portable and managed-host worktrees, use the catalog entry by its full path:

```sh
<catalog>/skills/github/scripts/dev-worktree retire --dry-run /exact/task/path
<catalog>/skills/github/scripts/dev-worktree retire /exact/task/path
```

Apply only after a passing preview. A held claim on the PR, an issue referenced
with `Refs`, `Fixes` or `Closes`, or a record naming the exact branch keeps the
related trees, even across repositories. Use `Refs OWNER/REPO#N` in the PR body
for delivery associations, or name every related branch on the existing
claimed work record. An ordinary URL in the body is not a recognized reference;
an unrecorded association is unprotected. Incomplete evidence or a kept target
returns nonzero.
The host lock's command is a legacy primitive marker, not ownership proof; never
substitute the bare host command. Preserve active, dirty or ambiguous work.

Explicit retirement supports completed or abandoned published tasks after
the existing closeout checks establish their disposition; it does not infer
completion from checkout age or merged/default history. The configured host
checks its current, legacy or missing registration lock and retains foreign
locks. Portable foreign locks remain holds. Physical publication, contents,
live-use and IDE checks still apply.

Creation checks each registered linked worktree through `landed_worktrees.py`.
A closed PR uses the existing PR-head, claim and publication checks;
commit-associated PRs cover detached review and separate refresh branches; a branch
without a PR uses the default-history, fresh-start and account-wide claim
checks. Active source/calling checkouts, trees without exact PR or default-history
provenance, foreign locks, dirty or unpublished work and incomplete evidence
are kept with reasons. The host command still owns volume, publication,
ignored-file, live-use and IDE checks. No lock is rewritten or bypassed.

Portable unlocked trees use live origin publication of HEAD and its reflog,
clean tracked/untracked/ignored state, live-use checks and the existing offline
IDE retirement proof. Missing tools or unsupported local proof keep the tree;
no force removal or fallback is used. Use `work-closeout` to resolve retained
contents or unavailable proof before retrying. Portable local branches remain
subject to the normal work-closeout branch policy. The same creation route runs on both
harnesses, without a scheduler or a host-specific landing handoff.

For an explicit repository pass, preview with:

```sh
uv run <catalog>/skills/github/scripts/landed_worktrees.py --repo OWNER/REPO --checkout /absolute/checkout --all
```

Add `--apply` to retire eligible trees. `--preserve /exact/path` keeps explicit
artifact custody during a bulk pass. Existing `--pr N` and
`--branch work/task` commands remain available for exact provenance. The
[daily stranded-work sweep](../../direction/references/stranded-work.md)
reports work that remains.

## Claude Desktop folder trust

Worktree location does not grant harness trust. The Desktop report in
[#1925](https://github.com/cbusillo/codex-skills/issues/1925) found that
`use_worktree: true` created `<repo>/.claude/worktrees/<name>` and required a
manual **Trust repository** click for each new folder even when the main
checkout was trusted. This is reported Desktop behavior, not a catalog-tested
automatic trust mechanism.

Start Desktop workers in the already trusted repository with automatic
worktree creation disabled (`use_worktree: false` where that launch option is
available). The worker claims the item and then creates its linked task
worktree with the catalog helper and host setting. If Desktop asks to trust
that worktree when opening or using it, the Director reviews the exact folder
and accepts the prompt manually. The helper neither grants trust nor promises
that a trusted main checkout or parent worktree root covers its children.
Launching in a trusted repository also does not establish access to a path
outside the session's launch directory; verify the worker can use the configured
worktree before planning unattended work there.
Before unattended Desktop launches into worktrees, verify that Desktop's own
location and trust controls support the chosen root; no unattended trust path
has been established here.

JetBrains `trustedAutoOpenRoots` names the source roots where native inspection
may run repository preparation; see
[native backend](../../jetbrains-inspection/references/native-backend.md).
It does not configure the worktree root or trust Claude Desktop. Codex's
registered-worktree trust handling is a separate harness behavior documented
in the [Supervisor launch helpers](../../supervisor/references/helpers.md).
