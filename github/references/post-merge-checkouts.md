# Post-Merge Local Checkouts

Read before evaluating or executing a local default-branch refresh, including
the explicit untracked-only exception. Remote merge success and local freshness
are separate outcomes.

After every confirmed merge, also inspect `git worktree list --porcelain` for a
unique local worktree already checked out on the repository's configured default
branch. This is a post-merge convenience-checkout refresh, not a source-selection
rule for agents. The active task worktree remains the authoritative agent source.
Apply refresh gates in this order: a runtime-bound checkout uses only the landed
reconciler and stops; any tracked dirt or active Git operation is report-only and
stops; only then may the explicitly requested untracked-only exception be
considered.

- If that default checkout is runtime-bound, use the landed repo-local runtime
  reconciler and do not run a generic pull there.
- If the active checkout is already that unique default worktree, evaluate it
  once rather than performing a duplicate refresh pass.
- Otherwise, verify the checkout shares the source repository's Git common
  directory and expected GitHub identity, remains clean and on the default
  branch, has a configured upstream, and is behind without being ahead or
  diverged. Fetch the configured upstream without switching worktrees and resolve
  it to an immutable `upstream_sha`, then record the current tip as `head_sha`.
  Require the exact final landing SHA to exist and appear on that pinned commit's
  first-parent history before mutation. Immediately before the merge, re-check
  that `HEAD` still equals `head_sha`, the tracked checkout is still clean, no Git
  operation is active, and `head_sha` remains an ancestor of `upstream_sha`. Use
  the same commit for every proof and the hook-disabled, no-autostash
  fast-forward: `git -C <path> -c core.hooksPath=/dev/null merge --ff-only
  --no-autostash --no-overwrite-ignore "$upstream_sha"`. Never pass a moving
  upstream-tracking ref directly as the merge operand; use only the pinned commit
  ID.
- Re-check that the default checkout is clean, `HEAD` equals `upstream_sha`, and
  the final `HEAD` contains the exact landing SHA. A fetch or fast-forward that
  does not prove those postconditions is not a successful refresh.
- If no unique default checkout exists, or it is dirty, ahead, diverged,
  detached, ambiguous, missing its upstream, or cannot fast-forward, do not
  switch, reset, stash, clean, or overwrite it. Report: `Local default checkout
  remains stale; fast-forward it before default-branch work or audits.`

A dirty checkout is never eligible for automatic refresh. If the user explicitly
requests this specific fast-forward, a non-runtime default checkout that is dirty
only because of untracked, non-ignored files may use a bounded exception:

1. The exception relaxes only untracked-file dirtiness. Every other surrounding
   precondition still applies: the checkout must remain on the configured default
   branch with a configured upstream, must share the source repository's Git
   common directory and expected GitHub identity, and must not be runtime-bound.
   Evaluate runtime binding before this exception and treat it as absolute:
   explicit user intent and untracked-only dirt never make a runtime-bound
   checkout eligible.
2. Fetch the configured upstream, resolve it to an immutable `upstream_sha`, and
   record the current tip as `head_sha`. Prove the branch is strictly behind that
   commit without divergence. Because this is a post-merge refresh, require the
   confirmed final landing SHA on `upstream_sha`'s first-parent history.
3. Prove the tracked index and worktree are clean. Fail closed if `git -C <path>
   status` reports an operation or if any path returned by `git -C <path> rev-parse
   --git-path` for `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `REBASE_HEAD`,
   `rebase-merge`,
   `rebase-apply`, `sequencer`, `BISECT_LOG`, or `BISECT_START` exists.
4. Enumerate every untracked entry with `git -C <path> ls-files --others
   --exclude-standard -z`, consume the NUL-separated output without shell
   globbing or pathspecs, and snapshot each path's file type and content or
   symlink-target hash. An entry ending in `/`, or any entry that cannot be
   fingerprinted as a regular file or symlink without descending into another
   repository, is ambiguous and must abort report-only.
5. Never pass a moving upstream-tracking ref directly as the merge operand; use
   only `upstream_sha`. Do not implement a separate path-collision predictor;
   Git's merge checks plus `--no-overwrite-ignore` must reject an incoming tracked
   path that would overwrite preserved work. Immediately before the merge,
   re-resolve `HEAD`, repeat the tracked-clean and operation-state checks, and
   abort report-only unless `HEAD` still equals `head_sha` and `head_sha` remains
   an ancestor of `upstream_sha`.
6. Fast-forward with `git -C <path> -c core.hooksPath=/dev/null merge --ff-only
   --no-autostash --no-overwrite-ignore "$upstream_sha"`.
7. Prove `HEAD` equals `upstream_sha`, `git -C <path> status` still has no
   tracked changes, and every untracked fingerprint matches its preflight value.
   Treat any nonzero merge, changed artifact, failed proof, or ambiguous result
   as a failed local refresh without undoing the remote merge.

The explicit request is not permission to stash, clean, stage, overwrite, or
include unrelated files. Runtime-bound checkouts remain ineligible and must use
their landed reconciler.

Keep this local-default result separate from the confirmed GitHub merge receipt.
A blocked local refresh does not undo or retry the merge, and it must never cause
an agent launched from a task worktree to substitute the default branch or a
remote ref for that active worktree.

