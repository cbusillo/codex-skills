# Git Safety

## Worktree Ownership

- Enumerate all worktrees before editing.
- Use a new linked worktree and focused task branch from the intended candidate
  head when another session owns the primary or shared candidate checkout.
- Record the exact real path, branch, and starting `HEAD` in evidence.
- Never reset, rebase, stash, clean, switch, or remove another session's state.
- Abandoning the isolated worktree is the rollback for a failed integration;
  shared history should not need repair.

## Provenance

- Validate the effective fetch URL, not merely the remote name.
- Pin full commit IDs after fetching. Never let a moving branch name define the
  recorded result.
- Verify the requested upstream commit is reachable from the canonical
  remote-tracking branch and is not older than the last recorded upstream.
- Record local remote-tracking provenance honestly. Do not claim signed or
  network-attested provenance unless separately verified.
- Reject shallow or incomplete history when the requested proof requires
  ancestry or snapshot reproduction; report unavailable evidence explicitly.

## Concurrency and Resources

- Respect the repository convergence lock and recheck `HEAD` before publishing
  generated evidence.
- Treat simultaneous agents, shared object stores, target directories, and disk
  space as shared resources even when worktrees are separate.
- Bound path lists and console output. Store full inventories in artifacts.
- Do not run automatic garbage collection, cleanup, force pushes, or destructive
  recovery during a refresh.

## Trust Boundary

- Read adapter, driver, workflow, dependency, generator, and build changes before
  executing candidate-controlled code.
- Do not pass credentials to unreviewed upstream hooks or package scripts.
- Reject repository-relative paths that resolve outside the real repository,
  including symlink escapes.
- Keep acquisition, structural inspection, merge application, and executable
  validation as separate phases so untrusted execution is never a prerequisite
  for understanding the change.
- Treat required checks, code-owner review, and workflow-change restrictions as
  external Git-host controls. Candidate-controlled validation cannot prove its
  own integrity when a change modifies the validator and its wiring together.
