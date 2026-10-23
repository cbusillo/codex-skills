# Task worktrees

Read before creating a task worktree or configuring a host's worktree root.
Claim issue-backed work with `github-plan` first. From any checkout, call the
catalog's helper by its absolute path; no PATH installation is needed:

```sh
<catalog>/skills/github/scripts/dev-worktree /absolute/repository task-slug origin/main
```

Here `<catalog>` is the checkout containing the loaded skills, not a literal
directory name. The command creates `work/task-slug` at the explicit starting
commit and prints the new path. It accepts a main checkout or a linked worktree;
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
`hostHelper` with an explicit `root`. The catalog command delegates its arguments
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
not install, replace or edit one.

Chris-Studio keeps its existing host-local helper and external-volume checks
with this configuration:

```json
{
  "root": "/Volumes/Developer-Artifacts/worktrees",
  "hostHelper": "/Users/cbusillo/.local/bin/dev-worktree"
}
```

This is a configuration example, not a claim that it has been applied. Until
that host setting is provisioned, continue using its existing host-local
command under the host's instructions. Do not run the portable default on that
host. The existing helper verifies the volume identity before creating anything,
locks registrations against unmounted-volume pruning, and names its guarded
retirement command in the lock reason. Keep that helper unchanged; never make a
replacement volume directory or fall back to internal storage. A catalog pull
does not provision host storage or change runtime configuration.

## Retirement

Use `work-closeout` to verify ownership, live use, contents and publication
before cleanup. Portable worktrees are ordinary unlocked Git worktrees: remove
an eligible finished one with `git worktree remove /exact/task/path`, without
force, then apply the normal branch cleanup policy. This helper adds no
portable deletion command. When a registration's lock names a retirement
command, use that exact command. With `hostHelper` configured, the catalog entry
also forwards `retire [--dry-run] /exact/task/path` to the host helper; it never
unlocks or deletes a worktree itself. Preserve active, dirty or ambiguous work.

The session that created a worktree usually ends before the merge train lands
its PR, so [After A Merge](../SKILL.md#after-a-merge) retires it with
`scripts/landed_worktrees.py`. That helper reads the PR, requires it merged or
closed, and keeps a worktree whose lock names no retire command or whose branch
has commits beyond the PR head. It also retires a review, conflict-refresh or
merge-train worktree on another branch when its HEAD is one of the PR's own
commits. It keeps one whose HEAD is on the default branch's first-parent
history, since a fresh worktree starts there, and one whose branch an open
claim on the PR or its closing issues names. The host helper's own checks
still decide.
Portable unlocked worktrees are outside it and follow work-closeout. The
[daily stranded-work sweep](../../direction/references/stranded-work.md) reports
finished worktrees the landing step left behind.

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

JetBrains `trustedAutoOpenRoots` authorizes IDE inspection opening only; see
[inspection lifecycle](../../jetbrains-inspection/references/lifecycle-diagnostics.md).
It does not configure the worktree root or trust Claude Desktop. Codex's
registered-worktree trust handling is a separate harness behavior documented
in the [Supervisor launch helpers](../../supervisor/references/helpers.md).
