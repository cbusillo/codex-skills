# Repository cleanup evidence

Read this reference when collecting or implementing structured cleanup evidence.
It defines the helper contract; the owning GitHub and closeout policies still
decide authorization, ownership, readiness, and whether an action is appropriate.
Apply [task scope and authorization](execution-scope.md). Reuse approval already
given for the same action and scope.

## Outcome and limits

`work-closeout/scripts/repo_cleanup.py` inventories one repository and explicitly
requested directories. Without `--root`, it inventories registered worktrees.
It can revalidate a private snapshot and verify specified postconditions after a
separately authorized action. It never removes, moves, cleans, resets, unlocks,
fetches, or changes repository files, branches, worktree registrations, or caches.
The only optional write is a new private manifest at the requested location.

Git-clean does not prove that ignored contents are disposable. The filesystem
probe enumerates ignored directories, including nested environment files, before
classifying them. It protects environment files, credentials, keys, databases,
private/recovery paths, tracked source and templates. Unrecognized local files
need review. A generated-file hint is not proof of ownership or disposability.

| Result | Meaning |
| --- | --- |
| Keep | A protection, boundary, active-use observation, or evidence gap applies. |
| Possible cleanup | No known hold or unclassified file was observed in the completed scope. Ownership, live use and authorization still need judgment. |
| Needs review | Local files have no established disposal policy. |
| Could not check | The requested directory could not be fully inspected. |

Every response has `policy.may_delete: false`. An exit code of zero means the
requested evidence was collected or compared successfully. It never means an
action is authorized or a candidate is safe to delete. Exit code 2 means invalid,
incomplete, stale, changed, or failed evidence. Do not turn an error into clearance.

## Inventory

From the installed catalog root, for example:

```sh
uv run work-closeout/scripts/repo_cleanup.py inventory --repo /path/to/repo --json
uv run work-closeout/scripts/repo_cleanup.py inventory --repo /path/to/repo \
  --root /path/to/task-output --preserve-root /path/to/installed-runtime --json
```

The versioned JSON includes:

- Repository/common-directory identity, live default-branch evidence, local
  heads/tags/stashes, worktree registrations, locks and Git operations. Remote
  advertisements are read with `ls-remote`; no fetch or tracking-cache substitution.
  Exact advertised objects or ancestry to a locally available live remote tip
  establish the reported ref coverage. Unknown coverage remains unknown.
- Every requested root's start/end timestamps, coverage, identity, entries,
  tracked/ignored/untracked classification, holds and unresolved questions.
  Coverage is `completed`, `excluded`, `unavailable`, `permission-denied`, or
  `timed-out`. Interrupts are recorded as stopped/timed-out observations.
- File type, size, identity and change metadata. Content and credential-bearing
  remote URLs are never printed. Counts and logical bytes are observations,
  not a claim about filesystem blocks reclaimable after deletion.
- Runtime bindings from `CODE_HOME`, then `CODEX_HOME`, then `~/.code`, active
  working directory, and explicit `--preserve-root` inputs. Other installed
  runtimes, apps and IDEs may remain unknown. Pass known protected locations;
  absence of a binding or an agent does not establish inactivity.
- Bounded `lsof` evidence when available. A positive PID observation is a hold.
  No result, missing tools, errors, incomplete access and unsupported platforms
  never establish that a directory is unused. Rechecking cannot discover every
  application, runtime lease or future user of a path.

Probes use POSIX descriptor-relative operations and do not follow descendant
symlinks. Explicit root aliases are recorded by physical device/inode identity
and their sizes are not double-counted. Aliases cannot be action targets.
Nested repositories, Git metadata directories, and mount boundaries are explicit
exclusions. The helper preserves the source/default/runtime checkout. A locked
worktree stays protected even if the lock appears stale.

Two filesystem passes and Git snapshots around the scan detect observed changes.
This is not an atomic filesystem snapshot or a deletion lease. Another process
can change a path after the check. The action owner must revalidate the exact
target immediately before mutation and preserve ambiguous or newly active state.
The helper does not provide an atomic check-and-delete operation.

Default bounds are 20,000 entries, 256 MiB of file bytes per content pass, depth
64, ten seconds per root, and sixty seconds overall. Git and process probes are
also bounded; inaccessible or hung roots do not disappear from coverage.
`--exclude RELATIVE/PATH` is a literal, explicitly reported pruning choice.
Backups, Trash and dependency trees have no silent pruning exception.
Pruned or offline inventory cannot serve as final revalidation evidence.
Use a smaller explicit scope and a new complete snapshot when a bound is reached.
Do not claim an all-disks audit from this repository-scoped report.

## Private manifests and retention

Inventory normally prints metadata without reading file contents. `--manifest`
enables content fingerprinting for verification, using a fresh random HMAC key.
The key and content/URL fingerprints exist only in the private manifest and are
omitted from both terminal and JSON output. Never publish the manifest, private
path inventory, key, fingerprints or raw Git/process errors in an issue or PR.
Publish only the necessary redacted conclusion and relevant source revision.

Choose an existing owner-only directory outside every scanned root. The helper
requires an owner-only parent and creates a new mode-0600 file without following
or overwriting an existing destination. Give it a purpose and a short expiry;
the default is one hour and the maximum is one day. There is no manifest cache
or automatic retention daemon. After the action and verified result are recorded,
remove only that task-owned evidence file through the normal cleanup policy.
Do not create manifests merely to retain routine terminal inventory.

```sh
uv run work-closeout/scripts/repo_cleanup.py inventory --repo /path/to/repo \
  --root /path/to/task-output --root /path/to/retained-local-data \
  --manifest /private/task-evidence/before.json \
  --purpose "Verify this task's approved cleanup, then remove this manifest" --json

uv run work-closeout/scripts/repo_cleanup.py revalidate \
  --before /private/task-evidence/before.json --json
```

Revalidation requires complete unpruned content evidence, an unexpired private
manifest, and by default a snapshot at most five minutes old. It compares exact
root identities, file contents/metadata and Git state. `snapshot_unchanged` is
only evidence of the comparison; all protection and live-use holds still apply.
Restart inventory after stale or changed evidence. Do not simply raise age or
scan limits until an unsafe result becomes green.

## Verify an external action

After an approved action, name only exact original manifest roots:

```sh
uv run work-closeout/scripts/repo_cleanup.py verify \
  --before /private/task-evidence/before.json --removed /path/to/task-output --json
uv run work-closeout/scripts/repo_cleanup.py verify \
  --before /private/task-evidence/before.json \
  --moved /path/to/local-data /path/to/durable-recovery --json
```

Removal requires the source to be absent; removing protected or ambiguous
contents fails verification. A recovery move requires source absence and a
complete destination with matching relative paths, bytes and permissions.
Copying may change device/inode identity, so the move comparison uses content
evidence. The comparison covers regular-file bytes, relative paths, POSIX modes
and owner/group; it does not qualify platform-specific ACLs, extended attributes
or resource forks. Preserve any such valuable state separately before an action
and do not call this byte comparison a complete backup verification. Filesystem
boundaries and active/protected runtime bindings cannot pass recovery verification.
A failed or incomplete cross-device copy cannot pass. Trash does not
qualify as the sole durable recovery copy. Neither Trash nor a successful move
proves reclaimed space; `space_reclaimed_bytes` remains unknown.

Other inventoried roots must retain their identity and contents, including
private files. Git refs, status, operations and unrelated registrations must
remain intact. Only the explicitly removed/moved worktree registration may
change; branch deletion is outside this verifier's contract. Use a new inventory
around a separate approved branch operation. Uninventoried paths are outside
the preservation claim, and this helper does not establish backup durability.

The synthetic helper tests exercise filesystem and Git contracts. They do not
prove that an ordinary Codex/Astra session selects this helper or obeys the
broader closeout workflow; qualify that integration separately.
