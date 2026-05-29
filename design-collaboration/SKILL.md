---
name: design-collaboration
description: Use when the user wants UI/UX design collaboration, Claude Design, Codex or Every Code design tools, first/second design passes, mockups, visual direction, critique, or an outside collaborator to draft or review UI before implementation. Use github-plan issues as the durable design record.
metadata:
  short-description: Issue-backed UI design collaboration
---

# Design Collaboration

Use this skill when visual style, UX direction, or an external design pass
should be coordinated before or alongside implementation.

Design collaboration is issue-backed. Use `github-plan` for durable design
state: product context, design requests, returned critique, accepted direction,
implementation constraints, PR links, browser QA evidence, tradeoffs, and
closeout state.

Do not create local handoff Markdown files. Repos should hold product and
implementation facts; GitHub planning issues should hold active design workflow
state.

## Core Split

External design collaborator owns:

- visual direction and mood
- composition and layout concepts
- typography and color direction
- interaction feel and polish
- making the surface not ugly

The Every Code harness owns:

- product context and user workflow
- required states and acceptance criteria
- technical constraints and existing repo patterns
- implementation feasibility
- browser validation, accessibility, responsive behavior, and final QA

## GitHub Issue Model

Use `github-plan` before creating or updating design work:

1. Think in chat first when the direction is still fuzzy.
2. Search existing planning/design issues before creating a new one.
3. Create or update one canonical issue when the design work should persist.
4. For broad redesigns, use a parent issue plus sub-issues for independent
   surfaces, states, implementation tracks, or validation work.
5. Use `Current Status` as the recovery point for future sessions.
6. Link implementation PRs with `Refs #123` unless auto-close is clearly
   intended and validation can conclusively finish the issue.
7. Before closeout, update the issue with accepted direction, evidence,
   remaining work, and stale/related issue cleanup.

Design issues should use the normal `github-plan` headings, with design-specific
content inside them:

- `Objective`: product goal, audience, target surface, and why the design work
  matters.
- `Finish Line`: observable done state for the designed and implemented UI.
- `Current Status`: state, next action, blocker or waiting condition, and last
  verification.
- `Scope`: included surfaces/states and explicit non-goals.
- `Acceptance Criteria`: functional, visual, responsive, accessibility, and
  implementation criteria.
- `Relationships`: parent/sub-issues, blockers, design comments, PRs, previews,
  and related docs.
- `Validation`: browser QA steps, screenshots, viewport checks, and evidence.
- `Decisions`: accepted design direction, tokens, interaction patterns, and
  intentional deviations from drafts.
- `Open Questions`: unresolved product, visual, technical, or ownership choices.

## Design Request Content

When asking Claude Design, Every Code, Codex, or another collaborator for a pass,
place the request in the canonical issue or an issue comment. Include only the
sections needed for the task:

- target collaborator and requested output: critique, visual direction, mockup,
  tokens, component plan, or implementation notes
- product/repo, audience, target surface, route, and relevant paths
- current problems, UX friction, and visual issues
- primary user goal, primary actions, secondary actions, and first-view priority
- required states: default, empty, loading, error, dense data, success,
  mobile/narrow, and role or mode variants
- technical constraints: framework, component system, styling system, assets,
  browser/device requirements, and things that cannot change
- visual direction: brand cues, references, things to avoid, and style freedom
- accessibility and usability needs: keyboard/touch, contrast/readability,
  motion sensitivity, density, and scanning needs
- requested response format: summary, hierarchy, tokens, state notes,
  assumptions, tradeoffs, and implementation guidance

For first-pass design work, bias toward product context, required states,
hierarchy, design tokens, and response format. Avoid over-prescribing framework
implementation unless it is a hard constraint.

For second-pass or implementation-prep work, convert the accepted design into
concrete UI tasks, preserve backend/API constraints, and note any intentional
departures from the draft in `Decisions`.

When the user has asked to read or prepare from a design brief and then
explicitly says to implement it, start coding, or move to implementation, move
into the work instead of asking for another confirmation unless a real blocker
or scope ambiguity remains. Do not treat a bare "go" or "go ahead" as
implementation approval by itself.

## Consuming Returned Design

Do not blindly trust returned UI/design output.

- Compare it against the issue acceptance criteria and required states.
- Preserve existing product and repo constraints when resolving conflicts.
- If the returned design is beautiful but incomplete, request the missing states
  in the issue or issue comments.
- If the returned design is not implementable, reduce it to the closest
  shippable version and record the tradeoff.
- Update `Decisions`, `Acceptance Criteria`, `Validation`, and `Current Status`
  instead of leaving conclusions only in chat.
- Use `browser-ui-review` after implementation and attach or reference browser
  evidence before signoff.

## Repo Documentation Boundary

Repository docs should not describe skill workflows or active design plans.

Keep repo docs for durable product and implementation facts such as:

- design tokens and component conventions
- route structure and target surfaces
- accessibility requirements specific to the product
- backend/API constraints that affect UI behavior
- accepted, stable UI policy that applies beyond one workstream

Do not add repo docs that say which skill to use, create local handoff files, or
mirror active issue checklists. If a repo contains stale design-handoff
instructions or legacy `handoff*.md` files, replace them with stable
product/repo facts or move active work into the canonical GitHub planning
issue.

## Workflow

1. Use `github-plan` to search for existing relevant planning issues.
2. Decide whether the design need is ephemeral chat or durable issue-backed
   work.
3. Create or update the canonical design issue when durable state is needed.
4. Add the design request or critique prompt to the issue or an issue comment.
5. Evaluate returned design against acceptance criteria, constraints, and
   required states.
6. Implement when the user explicitly asks for implementation, when the
   accepted direction is already captured and execution is the requested next
   step, or after an explicitly requested external design pass has been
   accepted. Do not treat a bare "go" or "go ahead" as sufficient approval to
   switch into implementation.
7. Validate with `browser-ui-review` across relevant interactions and viewports.
8. Update the issue with accepted decisions, evidence, PR links, and remaining
   work before closeout.
