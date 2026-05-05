---
name: design-handoff
description: Use when the user wants external UI/UX help, Claude Design, Codex or Every Code design tools, a design handoff, first/second design pass, mockups, visual direction, or an outside collaborator to draft or critique UI before implementation. Create gitignored handoff files; do not claim this harness can invoke external design tools directly.
---

# Design Handoff

Use this skill when visual style invention should happen outside this harness.
The external design collaborator owns visual direction; this harness owns product
context, constraints, implementation, browser QA, and acceptance.

This skill is especially appropriate for a two-pass design workflow:

1. Claude Design or another design-capable collaborator creates the first visual
   draft.
2. This harness evaluates the result against product/backend constraints,
   prepares any follow-up design handoff, then implements and browser-validates
   the shippable version.

If the user says they want Claude Design to make the first draft and Codex/Every
Code to make the second pass, use this skill automatically.

## Core Split

External design collaborator owns:

- visual direction and mood
- composition and layout concepts
- typography and color direction
- interaction feel and polish
- making the surface not ugly

This harness owns:

- product context and user workflow
- required states and acceptance criteria
- technical constraints and existing repo patterns
- implementation feasibility
- browser validation, accessibility, responsive behavior, and final QA

## Handoff Files

Use gitignored handoff files unless the user explicitly asks for committed docs.
Recommended filenames:

- `handoff-to-claude-design.md`
- `handoff-to-codex-design.md`
- `handoff-to-every-code.md`
- `handoff-to-designer.md`

Do not spend extra tool calls preflighting ignore rules. If a handoff Markdown
file appears in `git status --short` as untracked, add `handoff*.md` to the
repo-local exclude file so the ignore rule does not churn tracked `.gitignore`
files or PR diffs:

```bash
exclude_file="$(git rev-parse --git-path info/exclude)"
grep -qxF 'handoff*.md' "$exclude_file" || printf '\n%s\n' 'handoff*.md' >> "$exclude_file"
```

Only add a committed `.gitignore` rule when the user explicitly wants the
convention shared with the repository:

```gitignore
handoff*.md
```

Do not write secrets, credentials, private customer data, or unnecessarily
sensitive infrastructure details into a handoff file. When sensitive context is
needed, summarize the constraint safely.

## Workflow

1. Identify the target collaborator and handoff filename.
2. Inspect the repo docs, target files, existing UI, assets, and screenshots if
   available.
3. For Claude Design first-pass work, bias the handoff toward product context,
   required states, hierarchy, design tokens, and response format; avoid
   over-prescribing framework implementation details unless they are hard
   constraints.
4. Fill out a handoff using `assets/handoff-template.md`.
5. Ask the user to review or use the handoff with the external collaborator.
6. When design output returns, evaluate it before implementation:
   - Does it satisfy the workflow and required states?
   - Is it implementable in the stack and component system?
   - Does it preserve important repo/product constraints?
   - Does it cover desktop, mobile, empty/loading/error, and dense states?
   - Are controls discoverable and accessible?
7. For a Codex/Every Code second pass, convert the design into concrete UI tasks,
   note any departures from the draft, and preserve backend/API constraints.
8. Implement only after the user approves the direction or asks to proceed.
9. Validate implementation with `browser-ui-review` before signoff.
10. After the handoff has been consumed, remove the handoff file unless the user
   asks to keep it for another iteration. Migrate durable decisions into the
   relevant plan or repo docs before deleting the transient handoff.

## Handoff Quality Rules

- Be specific about the product problem, not prescriptive about style unless the
  user gave a style direction.
- Include existing screenshots or screenshot paths when available.
- Include concrete states and workflows, not just a static happy path.
- Ask for design tokens or implementation notes when that will make the result
  easier to build.
- Ask the collaborator to call out assumptions and tradeoffs.
- Request output in a format this harness can consume: markdown critique,
  design tokens, component plan, screenshots, or code diff notes.

## Consuming Returned Design

Do not blindly trust returned UI/design output.

- Compare the result against the handoff acceptance criteria.
- Preserve existing product and repo constraints when resolving conflicts.
- If the returned design is beautiful but incomplete, ask for missing states or
  make a small follow-up handoff.
- If the returned design is not implementable, reduce it to the closest
  shippable version and explain the tradeoff.
- Use browser screenshots as evidence before calling the implementation done.
- Treat handoff files as temporary working artifacts. Once the result has been
  accepted, implemented, rejected, or migrated into a follow-up plan, delete the
  handoff file so stale design direction does not influence later work.

## Template

Start from `assets/handoff-template.md` and remove sections that do not apply.
