# Codex Skills Repository

## What Codex Skills does

Codex Skills is the shared catalog of skills and helpers that lets coding agents finish repository work consistently on Codex and Claude Code.
It supplies the execution, review, planning, and handoff procedures; each product repository keeps its own product facts.
Evidence: [DIRECTION.md](DIRECTION.md).

## Direction and execution

- Follow the shared [executing loop](skills/references/executing-loop.md).

## Instruction maintenance

- Use [references/execution-scope.md](skills/references/execution-scope.md) when
  changing execution guidance. Preserve intentional approval, quality,
  delegation, and output-format policies.
- Maintain the top-level skill sources. The allowlisted system overrides are
  documented in [README.md](README.md#system-skill-overrides); generated
  `.system` caches and installed plugin caches are not development targets.
- For skill structure and metadata, follow the maintained
  [skill-creator authoring contract](skills/skill-creator/SKILL.md).
- Validate instruction-only changes with the existing skill structure, reference,
  behavior, and command-policy validators. Do not add tests that merely duplicate
  wording or run unrelated runtime suites without an affected behavior.

## Runtime Checkout Discipline

- Resolve the active skills directory using the runtime binding lookup in
  [README](README.md#runtime-binding-lookup). If a binding resolves into
  this repository, that exact
  worktree is a runtime checkout, not a development checkout.
