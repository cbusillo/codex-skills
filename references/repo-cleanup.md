# Repository cleanup and preservation

Read when closing out task artifacts, auditing repositories/worktrees for cleanup,
or retiring a checkout. This is the shared cleanup policy for `github` and
`work-closeout`, followed by the inventory helper contract. GitHub helpers still
own remote actions; readiness and closeout skills retain their quality and output
requirements.
Apply [task scope and authorization](execution-scope.md). Reuse approval already
given for the same action and scope. Fresh evidence does not require renewed
approval by itself.

## Scope and authority

| Request | Scope of work |
| --- | --- |
| Ordinary task closeout | Account for this task's outputs, branch and worktree. Remove authorized disposable output and eligible completed task checkouts; retain unrelated work. |
| Explicit bulk audit/cleanup | Inventory the requested repositories/directories, investigate possible supersession, and act only within the authorized disposal scope. A read-only audit stays read-only. |
| Repository retirement | Inventory the entire named checkout, including ignored/private files and installed consumers; preserve valuable state and resolve active ownership before the separately authorized removal. |

Inventory is not a deletion-candidate list. The helper always reports
`policy.may_delete: false`; neither `Possible cleanup` nor exit zero grants
authority. Local removal, publishing local work, deleting a remote branch, and
archiving/deleting a remote repository are separate actions. Reuse authorization
that covers each action; do not infer one from another. Ordinary development
does not trigger a global repository scan, cache sweep, or retirement workflow.

## Establish the disposition

1. Identify the exact targets and their owners. Preserve the primary/default
   checkout, installed runtime checkouts, active worktrees, review/automation
   worktrees, and shared caches unless a separate applicable lifecycle explicitly
   permits the intended action. A clean status, no agent session, or an old lock
   does not establish inactivity. Check current jobs, IDE leases and runtime
   bindings; unresolved ownership remains a hold. Release a known task-owned
   worktree lock only when its job has ended and removal is authorized, then
   collect fresh evidence. Never unlock merely because a lock looks stale.
2. Establish current Git evidence. Resolve the live default branch and pin its
   advertised SHA (`git ls-remote --symref`); obtain missing objects through an
   authorized fetch, rather than trusting a stale tracking ref. Check ancestry
   against that commit, local refs/stashes, unpublished commits, staged and
   unstaged diffs, and PR/issue provenance. Do not stop investigating merely
   because a branch is unmerged or a checkout is dirty.
3. Explain what happened to the work. Ancestry can prove committed history is
   present; it says nothing about dirty files. For squash/rebase/replacement
   cases, inspect patch equivalence (`git cherry` or stable patch IDs), tree
   differences, unique commits/hunks, original intent and the replacement's
   behavior or relevant checks. Patch similarity alone is not semantic
   supersession. Record which changes landed, were replaced, remain valuable,
   or are unresolved. Preserve unique or ambiguous work by default. Existing
   informed disposal authorization may cover known unwanted work; an uncertain
   interpretation of that authority does not.
4. Inventory filesystem contents, including ignored files, before removing a
   whole checkout. Use the helper below for structured inventory and preservation
   verification, with explicit roots matching this task. Review environment
   files, credentials, databases, recovery copies and IDE state even when Git
   says clean. Follow [IDE configuration policy](ide-configuration-policy.md):
   shared tracked settings remain durable; inspect mixed hunks individually.
   Ignored `.idea/` or `.vscode/` data is not automatically disposable. Whole-tree
   retirement needs a protected-file disposition, not just a merged branch.
5. Before removal, verify durable preservation of valuable local-only work, or
   confirm that existing informed authorization covers its disposal. Use
   [parking procedures](../work-closeout/references/parking-and-handoff.md) when
   work must survive: a verified pushed branch and owning issue, or an authorized
   durable local destination with reconstructable Git state, dirty patches and
   local files as needed. Record intent and the next adopt/discard step. A patch
   without needed base objects/files, a temporary manifest, reflog or Trash is
   not sufficient recovery. If no authorized durable route exists, retain the
   work and name the missing choice; continue independent authorized cleanup.
6. Immediately before each mutation, recheck exact path/device identity, current
   Git/filesystem state, ownership and live use. Honor the host's volume-identity
   and worktree-location policy; missing/wrong volumes, symlink escapes,
   unexamined boundaries and just-in-time changes remain holds. Do not substitute
   an internal-drive destination or follow a symlink to make a check succeed.
   Prefer non-force Git removal. Force syntax never substitutes for resolving a
   hold; use it only when necessary for the exact already-authorized disposition
   after preservation and ownership evidence is complete.
7. Verify the intended effect and preservation of the remaining state. Use the
   helper's revalidation/postcondition commands where its contract applies. A
   helper hold or failed verification is not a successful result. If a separately
   authorized action falls outside its contract (for example, deleting a branch
   or retiring a checkout after separately preserving private files), verify the
   exact refs/files independently and report the material limit. Never relabel a
   failed helper result as passed or suppress its protection evidence.

This procedure does not relax protected-branch rules, the pinned fast-forward
procedure, or the landed runtime-checkout reconciler. Merge success and runtime
reconciliation remain separate outcomes.

## Task artifacts and retention

When creating a task output directory, choose its purpose and expected lifetime
once for the group. At closeout, account for the actual files the task created;
a brief list in the existing task record is enough. No per-file issue, running
ledger, manifest or retention service is required.

| Class | Disposition |
| --- | --- |
| Disposable | Task-owned scratch or generated output whose purpose is complete; remove within existing authority after checking contents and live use. |
| Acceptance evidence | Keep what is still needed to substantiate review/acceptance; publish a redacted conclusion or suitable durable artifact before discarding the only evidence. |
| Recovery | Keep until the preserved work is adopted, explicitly discarded, or another verified durable copy replaces it. |
| Private configuration | Preserve; an ignored/generated name does not grant disposal authority. |
| Ambiguous | Retain until ownership, contents and intended use are resolved. |

For each retained group, record an owner, reason and review/disposal trigger
(such as acceptance recorded or owner adopts the parked change), not an arbitrary
expiration. Use the existing issue/PR for public-safe summaries. A necessary
private record belongs in an owner-only local location outside removal targets,
with mode 0600; never commit or publish private paths, contents or raw manifests.
Temporary helper-manifest validity below is independent of valuable-artifact
retention. Do not gate routine disposable-output removal on issue capability,
remote publication, or parking checks.

Report what was **removed**, **retained** (with reason/trigger), **blocked**
(specific evidence or authority missing), and **unexamined** (coverage limits).
Keep the report proportional; omit empty detail. A completed local cleanup does
not establish a full scan or release readiness. Excluded backups, inaccessible
roots, interruptions and timeouts must remain visible; none means "all clean."
Base coverage claims on actual traversal and probes of the named roots. Reading
representative files or a prior record does not prove a complete scan or a current
access failure; label unverified state as reported or unexamined.

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
