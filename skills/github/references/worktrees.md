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
helper owns enforcement of the configured root: use the root that it actually
implements. This setting selects an existing trusted host executable; it does
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
Before unattended Desktop launches into worktrees, verify that Desktop's own
location and trust controls support the chosen root; no unattended trust path
has been established here.

JetBrains `trustedAutoOpenRoots` authorizes IDE inspection opening only; see
[inspection lifecycle](../../jetbrains-inspection/references/lifecycle-diagnostics.md).
It does not configure the worktree root or trust Claude Desktop. Codex's
registered-worktree trust handling is a separate harness behavior documented
in the [Supervisor launch helpers](../../supervisor/references/helpers.md).
