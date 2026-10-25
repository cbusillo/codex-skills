# Codex Skills Repository

## What Codex Skills does

Codex Skills is the shared catalog of skills and helpers that lets coding agents finish repository work consistently on Codex and Claude Code.
It supplies the execution, review, planning, and handoff procedures; each product repository keeps its own product facts.
Evidence: [DIRECTION.md](DIRECTION.md).

## Direction and execution

- Follow the shared [executing loop](skills/references/executing-loop.md), loading
  each step's owning skill before acting. Claim issue-backed work with
  `gh-plan.py claim` before creating its linked task worktree.

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
- For test changes, read the [shared test rules](instructions/global.md#tests).
- New Python comments and docstrings follow
  [Names Instead Of Comments](skills/references/python-odoo-style.md#names-instead-of-comments);
  `uv run scripts/check_new_comments.py --base origin/main` runs the CI check locally.

## Runtime Checkout Discipline

- Resolve the active skills directory using the runtime binding lookup in
  [README](README.md#runtime-binding-lookup). If a binding resolves into
  this repository, that exact
  worktree is a runtime checkout, not a development checkout.
- Keep the runtime checkout clean, on the repository default branch, and current
  with its remote. Perform implementation work in focused linked worktrees.
