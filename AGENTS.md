# Codex Skills Repository

## Instruction maintenance

- Use [references/execution-scope.md](skills/references/execution-scope.md) when
  changing execution guidance. Preserve intentional approval, quality,
  delegation, and output-format policies.
- Before merging a change to execution guidance, approval or safety rules, or
  a destructive helper, get a review by another model and weigh it as described
  in [references/model-review.md](skills/references/model-review.md).
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

## Tests

- Keep a test only if it fails when the product breaks and passes when someone
  makes an intended change.
- Never assert a literal defined elsewhere, such as a version, toolchain, hash,
  policy id, or instruction wording. Assert agreement with the one source of
  truth, or assert nothing.
- Never assert workflow or config text. Enforce the rule where it runs: in the
  workflow, in a helper with its own tests, or in a linter.
- Verification code must not depend on working-tree state such as untracked
  files or the host's installed runtime. Read tracked files or fixtures.
- Byte-exact and hash gates belong only on real artifacts and immutable
  evidence.

## Runtime Checkout Discipline

- Resolve the active skills directory with `CODE_HOME`, then `CODEX_HOME`, then
  `~/.code`, then `~/.agents/skills` (whole-catalog link) or
  `~/.agents/skills/shared` (installer binding), then any catalog linked under
  Claude Code's `skills` folder. If one of those resolves into this repository, that exact
  worktree is a runtime checkout, not a development checkout.
- Keep the runtime checkout clean, on the repository default branch, and current
  with its remote. Perform implementation work in focused linked worktrees.
- After a confirmed merge affecting this repository, run the landed repo-local
  `skills/github/scripts/reconcile-runtime-checkout.py` helper with the final landing
  SHA. Treat remote merge success and local runtime reconciliation as separate
  outcomes.

  ```sh
  uv run skills/github/scripts/reconcile-runtime-checkout.py \
    --merged-worktree "$PWD" \
    --repo OWNER/REPO \
    --landing-sha <full-landing-sha>
  ```

- Never switch, reset, stash, clean, or overwrite an unsafe runtime checkout as
  part of automatic reconciliation. Preserve unexpected work separately and
  report the blocker.
- Runtime-dependent evidence is current only when its recorded helper/source
  revision matches the intended landed runtime revision.
