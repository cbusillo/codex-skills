# Codex Skills Repository

## Runtime Checkout Discipline

- Resolve the active skills directory with `CODE_HOME`, then `CODEX_HOME`, then
  `~/.code`. If its `skills` path resolves into this repository, that exact
  worktree is a runtime checkout, not a development checkout.
- Keep the runtime checkout clean, on the repository default branch, and current
  with its remote. Perform implementation work in focused linked worktrees.
- After a confirmed merge affecting this repository, run the landed repo-local
  `github/scripts/reconcile-runtime-checkout.py` helper with the final landing
  SHA. Treat remote merge success and local runtime reconciliation as separate
  outcomes.

  ```sh
  uv run github/scripts/reconcile-runtime-checkout.py \
    --merged-worktree "$PWD" \
    --repo OWNER/REPO \
    --landing-sha <full-landing-sha>
  ```

- Never switch, reset, stash, clean, or overwrite an unsafe runtime checkout as
  part of automatic reconciliation. Preserve unexpected work separately and
  report the blocker.
- Runtime-dependent evidence is current only when its recorded helper/source
  revision matches the intended landed runtime revision.
