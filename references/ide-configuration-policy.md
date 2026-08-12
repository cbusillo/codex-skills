# IDE Configuration Policy

Use this policy when IDE configuration appears in `git status`, a diff, review,
readiness check, or closeout. JetBrains paths are the primary example, but the
same rules apply to `.vscode/` and equivalent project configuration.

## Classify State First

Classify each IDE path before staging, editing, reverting, or deleting it:

- **Tracked:** inspect the exact diff and preserve the repository's intended
  shared form.
- **Untracked and ignored:** leave generated local state untracked; do not
  force-add or delete it as routine cleanup. Preserve it unless the user
  explicitly requests deletion of that specific known local artifact.
- **Untracked and not ignored:** do not stage automatically. Check repository
  policy and propose an ignore or sharing decision when needed.

Read the exact diff, applicable `AGENTS.md` and `.github/github.json`, relevant
`.gitignore` files such as `.idea/.gitignore`, and the file's Git history. A
tracked file remains tracked even when a broader ignore rule also matches it.

## Classify Hunks, Not Files

Safe shared settings are repository-relative and reproducible. Examples include
inspection profiles, scopes, code style, canonical module or content roots,
and project-only VCS mappings whose resolved paths stay inside the repository.

Machine-local state must stay out of commits. Examples include absolute paths,
sibling-repository mappings such as `$PROJECT_DIR$/../other-repo`, generated
checkout or worktree paths, local SDK or interpreter paths, recent-file state,
caches, credentials, and `workspace.xml`.

Treat unclear hunks as machine-local until evidence or user direction shows
they are shared project configuration.

## Preserve The Canonical Shared Form

When a tracked IDE file mixes safe and machine-local hunks, rebuild it as the
canonical shared form plus only the safe hunks. Use a targeted edit,
`git restore -p`, or a reduced patch. Do not blanket-commit the generated file,
blanket-revert the file or IDE directory, clean ignored IDE state, or stash
unrelated changes to make the diff disappear.

Keep IDE configuration changes on the task branch, call intentional shared
changes out in the PR, and never overwrite unrelated local IDE changes. Starting
to track previously ignored IDE state is a durable repository-policy change and
requires explicit user direction.

## Readiness And Closeout

An unresolved tracked IDE configuration diff is not clean readiness evidence.
Resolve it into shared configuration, preserved local state, or an explicit
blocker before declaring the work ready. During closeout, tracked IDE
configuration is durable repository state, while ignored generated IDE state is
local state to preserve rather than a transient artifact to delete. Do not
delete ignored IDE state merely because the workstream is closing.
