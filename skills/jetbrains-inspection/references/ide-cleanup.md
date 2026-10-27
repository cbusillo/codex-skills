# Desktop IDE cleanup

Read before running `remove-worktree`, `cleanup-helper-sdks` or
`cleanup-helper-leases`. Commands are relative to the `jetbrains-inspection`
skill directory.

Agents no longer inspect through the desktop IDE. The retired desktop route
left state behind: local lifecycle leases, Python SDKs it registered in the
Director's IDEs, sealed project caches, and repository-preparation receipts.
The standalone lease and SDK cleanup commands retire that state through an IDE
whose Inspection API plugin is already running. They never open a project, launch
an app or inspect. Worktree removal uses local proof only. All commands run under
the helper's lifecycle lock. Native assessments create none of this state.

## Removing a worktree

`remove-worktree --repo /exact/task/path` runs after the repository cleanup
checks establish disposition. It never discovers or contacts a desktop IDE.
It refuses primary checkouts, locked apply attempts and uncommitted work.
It reconciles only leases for that exact worktree whose staleness and local
records prove no project needs closing. Leases that need live ownership proof
retain the worktree; the refusal names the blocking lease IDs and scoped cleanup
commands. Resolve those separately before a fresh retirement attempt.

On macOS, an unprepared worktree proves there is nothing to clean from local
preparation records, every standard JetBrains config's SDK and recent-project
records (plus the explicit `JETBRAINS_INSPECTION_IDE_CONFIG_DIR` override),
sealed/finalizing project-cache ownership markers, and running IDE open files.
A `.venv` alone is not a registration. Matching records, unreadable/malformed records, storage redirects
in config `idea.properties`, or local `.idea` project-model/workspace state
retain the worktree. A running supported IDE retains it only when `lsof` shows
an open file or working directory at or below the worktree in the launcher or
any descendant, or process/open-file
discovery cannot prove complete, stable coverage. Failed commands, warnings,
missing process records and process changes during proof retain it.
The remover's own process and observer children are excluded; ancestor terminal
shells and other IDE descendants are still checked.
Native workers receive the same check on their open paths under private snapshots,
without an exemption based on launcher names or a guess about their role.
Open-file and persisted-record checks cannot inventory in-memory projects or
unsaved editor buffers before the IDE writes its records. They do not replace
the caller's cleanup disposition and inactivity checks; uncertain human use
requires retaining the worktree. Existing desktop state needs separate
cleanup; retirement never edits global IDE files or removes project caches.
Other platforms retain the worktree because local proof is unavailable.
The scan covers standard macOS storage plus the explicit config override;
redirects in VM options, environment variables or edited application bundles
are not discovered. Keep those worktrees until their cleanup can be proved.

With `--no-dry-run`, local Git, lease and offline checks repeat before non-force
Git removal. Receipts report `offline_cleanup_proved` and
`offline_records_checked`. `--offline-only` remains accepted for existing
callers; it also retains every existing target lease without reconciliation.
When Git unregisters the worktree but cannot empty it because Finder wrote
`.DS_Store` files during the delete, the command deletes what is left only if
every entry is `.DS_Store` or unchanged since before removal; otherwise it
keeps them all and lists the others in `retained_entries`. The branch is
preserved. Prior cleanup disposition must also preserve unique detached commits
and valuable ignored files; this command does not classify them.

## Orphaned SDKs

`cleanup-helper-sdks` dry-runs the plugin's inventory of helper-owned SDKs whose
worktree is gone. Review the listed entries, then apply with `--no-dry-run` and
a repeated `--worktree-path /reviewed/orphan` for each one. Apply is limited to
paths still present in the current preview; refused orphan records stay listed
and do not block unrelated eligible paths. Start each IDE that prepared the
removed worktrees first; discovery covers running IDEs, not offline SDK tables.
Legacy SDK names or paths are never ownership proof.

## Stale leases

`cleanup-helper-leases` reconciles leases left by helper-opened desktop
projects. It closes a project only when the live plugin proves the lease owns
it; a definitive `not_owned` answer releases the local lease without a close,
and unavailable or legacy proof stays an explicit nonzero `unresolved` result.
It may also release a local lease without closing when the recorded IDE
process is definitively dead and no live route resolves the target, or when the
original IDE session answers a read-only probe with `project_not_open`. A live
helper PID always retains its lease.

To recover one known lease, scope both the dry-run and the apply with
`--lease-id <UUID>`. The selector normalizes one UUID (including uppercase or
hyphenless input) and filters before staleness checks and route discovery.
Malformed or repeated selectors, unknown IDs, duplicate lease identities and
filename/identity mismatches return nonzero as `invalid_lease_selector`,
`lease_not_found`, `lease_identity_ambiguous` or `lease_identity_mismatch`
without contacting an IDE. Plain output names preview or apply, the predicted,
removed and unresolved counts, and one `LEASE_UNRESOLVED` line per retained
lease with its reason and next action. Do not use a global sweep to recover one
project.
