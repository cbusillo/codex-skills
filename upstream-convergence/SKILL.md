---
name: upstream-convergence
description: Use when asked to update, refresh, synchronize, converge, or forward-port a maintained downstream fork against its upstream repository, including recurring upstream snapshot integrations and guarded fork cutovers. Do not use for an ordinary feature-branch merge, a one-off cherry-pick, or discussion of an upstream API that does not change the fork's upstream baseline.
metadata:
  short-description: Safely synchronize a maintained fork upstream
resources:
  - path: references/repo-adapter.md
    kind: reference
    description: Defines the small repository discovery manifest and phased command interface.
  - path: references/git-safety.md
    kind: reference
    description: Details worktree, provenance, concurrency, trust, and rollback boundaries.
  - path: references/review-handoff.md
    kind: reference
    description: Defines the bounded review and handoff evidence contract.
commands:
  - name: inspect-upstream-convergence
    source: repo
    example_argv:
      - python3
      - .github/scripts/upstream_convergence.py
      - inspect
      - --base
      - <full-merge-base-sha>
      - --upstream
      - <full-upstream-sha>
      - --local
      - <full-local-sha>
      - --json
    purpose: Produces a read-only conflict, residual, contract-lane, and provenance report.
  - name: record-upstream-convergence
    source: repo
    example_argv:
      - python3
      - .github/scripts/upstream_convergence.py
      - record
      - --base
      - <full-merge-base-sha>
      - --upstream
      - <full-upstream-sha>
      - --local
      - <full-local-sha>
      - --json
    purpose: Atomically appends one repository-owned evidence snapshot after inspection.
  - name: validate-upstream-convergence
    source: repo
    example_argv:
      - python3
      - .github/scripts/upstream_convergence.py
      - validate
      - --against
      - <full-review-base-sha>
      - --json
    purpose: Validates governance, guards, snapshots, provenance, and append-only history.
---

# Upstream Convergence

Safely move a maintained fork to a pinned upstream commit without silently
discarding product-owned behavior or turning historical commit archaeology into
the recurring workflow.

## Success Evidence

A completed refresh reports:

- the exact worktree, branch, starting `HEAD`, merge base, local commit, upstream
  commit, canonical remote URL, and remote-tracking tip;
- conflict and residual counts by repository-defined lane and contract;
- governance changes, guard violations, waiver counts, and stale waivers;
- one append-only evidence snapshot for a new upstream range;
- repository-defined tests and builds run only after the structural review;
- remaining product decisions, blockers, and the next safe action.

Never describe a refresh as complete merely because it compiles or merges.

## Authority Boundary

The shared skill owns trigger recognition, safe orchestration, phase boundaries,
reporting, and handoff. The repository owns all product policy and enforcement:

- upstream identity and adapter metadata;
- path lanes, contract IDs, and conflict semantics;
- snapshots, guard manifests, waivers, and historical anchors;
- exact tests, builds, CI, release gates, and branch policy.

Do not copy repository-specific rules into this skill. Read root `AGENTS.md`,
then locate `upstream/convergence-policy.json` and the repository-local driver.
Read `references/repo-adapter.md` before executing that driver.

## Workflow

### 1. Orient

1. Read applicable `AGENTS.md` files and the active GitHub plan.
2. Identify the current branch, repository default branch, all linked worktrees,
   dirty state, in-progress Git operations, remotes, and existing refresh owner.
3. Verify the adapter schema, repository-relative paths, canonical upstream
   identity, and repository driver before executing candidate-owned code.
4. If the repository is GitHub-backed, use `$github-plan` for durable status and
   `$github` for PR operations. Chat plans are execution aids, not recovery state.

### 2. Isolate

Create or select a focused linked worktree from the intended integration head.
Never edit the default branch, a shared release/candidate branch, the primary
checkout, or another session's worktree. Never reset, stash, clean, switch, or
delete another session's state.

Read `references/git-safety.md` when worktrees, concurrent sessions, remote
identity, provenance, resource pressure, or rollback are relevant.

### 3. Pin and Inspect

1. Confirm the configured upstream remote URL before fetching.
2. Fetch without changing the current branch, then resolve the merge base,
   upstream target, and local baseline to full immutable commit IDs.
3. Run the repository driver's `inspect` phase with those exact IDs.
4. Review governance, red/manual, and contract-adapted paths before merging.
5. Treat green paths as upstream-owned unless the repository names a contract.

Do not make commit-by-commit inspection the normal integration unit. Use it only
for a bounded red/manual contract or forensic question that the range inventory
cannot answer.

### 4. Apply

Apply the pinned upstream range in the isolated worktree. Preserve upstream
behavior by default, synthesize only repository-named contracts, and resolve
governance conflicts manually. Do not auto-resolve product identity, state-home,
authentication, release, model-default, or other red/manual boundaries.

Review source and build-system changes before running candidate-provided builds,
hooks, generators, or package scripts. Structural inspection is safe to perform
first; untrusted execution is not.

### 5. Record and Validate

Use the repository driver's phases rather than rewriting Git plumbing:

- `record` writes one new snapshot atomically and refuses overwrite;
- `validate` checks governance wiring, semantic guards, snapshot structure and
  reproducibility, and historical snapshot mutation against a pinned base;
- repository tests and builds then validate the named product contracts.

Never regenerate a pre-anchor ownership baseline from the current candidate.
Never rewrite an existing snapshot to match a newer classifier. Version the
policy and preserve the historical evidence instead.

### 6. Review and Publish

Use independent agents for semantic-contract, security/provenance, and
operations/release review when those surfaces changed. Give reviewers exact
worktree and commit provenance; discard reviews performed against the wrong
checkout.

Commit and push only a focused task branch. Open or update a PR rather than
pushing directly to the default or shared candidate branch. Read
`references/review-handoff.md` before updating durable status or PR evidence.

When a repository adds routing to this skill in the same rollout, land the
shared skill first and reconcile the active runtime skills checkout before
landing the repository route.

## Refuse or Stop

Refuse write phases when any of these are true:

- the adapter is missing, unknown, unsafe, or points outside the repository;
- the worktree is dirty, detached, primary/shared, on a protected branch, or
  already running a merge, rebase, cherry-pick, or revert;
- refs are symbolic or ambiguous instead of full commits;
- the upstream URL does not match the adapter, the commit is unreachable from
  the configured tracking ref, or upstream history moved backward;
- another convergence operation holds the repository lock;
- an existing snapshot would be overwritten or historical evidence changed;
- the immutable guard baseline would be advanced as part of routine refresh;
- governance files would be auto-resolved or CI enforcement weakened;
- a build requires executing unreviewed upstream code, unavailable credentials,
  or unbounded disk, network, time, or output.

Ask the user only for a genuine product-policy decision, unavailable credential,
or irreversible live-system authorization. Resolve ordinary Git, test, and
review work autonomously.

## Result Contract

Return a bounded status containing exact SHAs and provenance, conflict/residual
counts, affected lanes/contracts, governance changes, guard and waiver counts,
tests performed, PR/check state, blockers, and the next safe action. Keep large
path sets in generated artifacts rather than terminal output.
