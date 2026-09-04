# Codex Skills Repository

## Instruction maintenance

- Use [references/execution-scope.md](references/execution-scope.md) when
  changing execution guidance. Preserve intentional approval, quality,
  delegation, and output-format policies.
- Maintain the top-level skill sources. The allowlisted system overrides are
  documented in [README.md](README.md#system-skill-overrides); generated
  `.system` caches and installed plugin caches are not development targets.
- Keep activation rules, essential constraints, and the primary procedure in
  `SKILL.md`. Move substantial mode-specific detail into linked references with
  an explicit read condition. Preserve supported frontmatter and command-policy
  metadata consumed by tooling when reorganizing prose.
- Validate instruction-only changes with the existing skill structure, reference,
  behavior, and command-policy validators. Do not add tests that merely duplicate
  wording or run unrelated runtime suites without an affected behavior.

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
