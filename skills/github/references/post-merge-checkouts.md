# Local Checkout Refresh

Read before refreshing any local checkout: the active branch during closeout,
the repository's local default checkout after a merge, or a checkout the user
explicitly asks to fast-forward despite untracked files. This file owns those
procedures. Remote merge success and local freshness are separate outcomes.

## Gates

Apply the gates in this order and stop at the first that applies:

1. **Runtime-bound checkout**: use only the landed repo-local runtime
   reconciler with the confirmed final landing SHA: `merge.sha` from a direct
   merge or `mergeCommitOid` from a fresh merged-PR view, never a PR head,
   candidate, or pre-merge base. Never run a generic pull there. A blocked or
   failed reconciliation prevents claiming that installed runtime behavior or
   provenance-sensitive evidence is current; it does not reclassify or retry the
   confirmed merge.
2. **Tracked changes or an active Git operation**: report only; change nothing.
3. **Untracked-only dirt**: eligible only for the
   [explicit exception](#explicit-untracked-only-exception).

Ahead, diverged, detached, missing-upstream, or ambiguous state is report-only
at every step. Never switch, reset, stash, clean, stage, or overwrite a checkout
to make it eligible.

## Pinned Fast-Forward

Every refresh below uses this procedure:

1. Fetch the configured upstream without switching worktrees, resolve it to an
   immutable `upstream_sha`, and record the current tip as `head_sha`. Prove
   `head_sha` is an ancestor of `upstream_sha` without divergence.
2. Immediately before the merge, re-check that `HEAD` still equals `head_sha`,
   the checkout is clean with no tracked or untracked changes (only the
   explicit exception allows untracked files), no Git operation is active, and
   `head_sha` remains an ancestor of `upstream_sha`.
3. Run `git -C <path> -c core.hooksPath=/dev/null merge --ff-only
   --no-autostash --no-overwrite-ignore "$upstream_sha"`. Never pass a moving
   upstream-tracking ref as the operand; use only the pinned commit ID.
4. Prove `HEAD` equals `upstream_sha` and the checkout is still clean in the
   same sense. A fetch or merge that does not prove these postconditions is not
   a refresh.

## Active Branch During Closeout

When the active branch is clean, with no tracked or untracked changes, not
runtime-bound, and behind its configured
upstream, use the pinned fast-forward. This refresh has no landing SHA to prove
and must not invent one. If a proof fails, leave the branch as it is and report
its state and the next safe action.

## Default Checkout After A Merge

After every confirmed merge, inspect `git worktree list --porcelain` for a
unique local worktree already checked out on the configured default branch.
This is a convenience refresh; the active task worktree remains the
authoritative agent source and is never replaced by the default checkout or a
remote ref. If the active checkout is already that worktree, evaluate it once.

For a checkout that is not runtime-bound, verify that it shares the merged
worktree's Git common directory and expected GitHub identity, is clean and on
the default branch, and has a configured upstream. Then use the pinned
fast-forward, and also require the exact final landing SHA on
`upstream_sha`'s first-parent history before the merge and in the final
`HEAD` afterward.

If no unique default checkout exists, or any gate or proof fails, leave it
untouched and report: `Local default checkout remains stale; fast-forward it
before default-branch work or audits.`

## Explicit Untracked-Only Exception

A dirty checkout is never refreshed automatically. When the user explicitly
requests this specific fast-forward, a non-runtime checkout that is dirty only
because of untracked, non-ignored files may still be refreshed:

1. The exception relaxes only untracked-file dirtiness. The checkout must
   remain the identified repository on its configured default branch with a
   configured upstream; a separate default worktree must share the source
   repository's Git common directory and expected GitHub identity. Runtime
   binding is evaluated first and is absolute: explicit intent and
   untracked-only dirt never make a runtime-bound checkout eligible.
2. Resolve `upstream_sha` and `head_sha` as in the pinned fast-forward. When
   this exception serves the post-merge refresh, require the confirmed landing
   SHA on `upstream_sha`'s first-parent history; a closeout refresh without a
   merge has no landing SHA and must not invent one.
3. Prove the tracked index and worktree are clean. Fail closed if
   `git -C <path> status` reports an operation or if any path returned by
   `git -C <path> rev-parse --git-path` for `MERGE_HEAD`, `CHERRY_PICK_HEAD`,
   `REVERT_HEAD`, `REBASE_HEAD`, `rebase-merge`, `rebase-apply`, `sequencer`,
   `BISECT_LOG`, or `BISECT_START` exists.
4. Enumerate every untracked entry with `git -C <path> ls-files --others
   --exclude-standard -z`, consume the NUL-separated output without shell
   globbing or pathspecs, and snapshot each path's file type and content or
   symlink-target hash. An entry ending in `/`, or any entry that cannot be
   fingerprinted as a regular file or symlink without descending into another
   repository, is ambiguous and aborts report-only.
5. Do not predict path collisions separately; Git's merge checks plus
   `--no-overwrite-ignore` must reject an incoming tracked path that would
   overwrite preserved work. Immediately before the merge, re-resolve `HEAD`,
   repeat the tracked-clean and operation-state checks, and abort report-only
   unless `HEAD` still equals `head_sha` and `head_sha` remains an ancestor of
   `upstream_sha`. Then run the pinned fast-forward's merge command.
6. Prove `HEAD` equals `upstream_sha`, `git -C <path> status` has no tracked
   changes, and every untracked fingerprint matches its preflight value.

Any nonzero merge, changed fingerprint, failed proof, or ambiguous result is a
failed local refresh, reported without undoing or retrying the remote merge.
The request is not permission to stash, clean, stage, overwrite, or include
unrelated files.
