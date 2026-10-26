# Desktop IDE cleanup

Read before running `remove-worktree`, `cleanup-helper-sdks` or
`cleanup-helper-leases`. Commands are relative to the `jetbrains-inspection`
skill directory.

Agents no longer inspect through the desktop IDE. The retired desktop route
left state behind: local lifecycle leases, Python SDKs it registered in the
Director's IDEs, sealed project caches, and repository-preparation receipts.
These commands retire that state. They talk only to an IDE whose Inspection API
plugin is already running, never open a project, launch an app or inspect, and
run under the helper's lifecycle lock. Native assessments create none of this
state.

## Removing a worktree

`remove-worktree --repo /exact/task/path` runs after the repository cleanup
checks establish disposition. It refuses primary checkouts, locked apply
attempts and uncommitted work, then reconciles only the leases recorded for
that exact worktree; an unresolved or live lease retains the worktree and the
refusal names each blocking lease ID with its scoped `cleanup-helper-leases
--lease-id <UUID> --no-dry-run` command. It then dry-runs SDK and project-cache
retirement through every discovered running IDE.

With no running IDE on macOS, an unprepared worktree can instead prove there is
nothing to clean from local preparation records, every standard JetBrains
config's SDK and recent-project records (plus the explicit
`JETBRAINS_INSPECTION_IDE_CONFIG_DIR` override), and sealed/finalizing
project-cache ownership markers. A `.venv` alone is not a registration.
Matching records, unreadable/malformed records, storage redirects in config
`idea.properties`, or local `.idea` project-model/workspace state retain the
worktree for the live IDE cleanup route. Offline proof is repeated before Git
removal and reported as `offline_cleanup_proved`, with `offline_records_checked`
naming the records read. Add `--offline-only` when the session must not contact
an IDE; preview and apply then skip identity discovery and use the same local
proof, and existing leases hold the worktree without reconciliation. A running
supported IDE holds offline retirement; keep the worktree until a session that
may contact the IDE can use the live route. The offline scan covers standard
macOS storage plus the explicit config override; redirects in VM options,
environment variables or edited application bundles are not discovered and
need live cleanup proof. It never edits global IDE files. Other platforms
retain the live-IDE requirement.

With `--no-dry-run`, the command unregisters only SDKs proved helper-owned,
sending the apply only to IDEs whose preview lists one (`sdk_apply_skipped`
names the rest), rechecks every IDE, retires sealed project caches, then
removes the clean, unlocked linked worktree with non-force Git removal. Close
its IDE project first. Missing plugin support, session drift and unsafe SDK
cleanup retain the worktree. Unrecorded SDKs stay registered and are reported
as preserved; their `not_helper_owned` refusal does not prevent removal. When
Git unregisters the worktree but cannot empty it because Finder wrote
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
