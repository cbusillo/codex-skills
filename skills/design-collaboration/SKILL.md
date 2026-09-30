---
name: design-collaboration
description: Use when the user wants UI/UX design collaboration, Claude Design, Codex design tools, first/second design passes, mockups, visual direction, critique, or an outside collaborator to draft or review UI before implementation. Use github-plan issues as the durable design record.
metadata:
  short-description: Issue-backed UI design collaboration
---

# Design Collaboration

Use this skill when visual style, UX direction, or an external design pass
should be coordinated before or alongside implementation.

Use `github-plan` for durable design state and
`../references/talking-with-the-owner.md` when drafting requests, issue
comments, critique summaries, and closeout notes. Active workflow state belongs
in GitHub issues; repository docs hold product and implementation facts. Do not
create local handoff Markdown files.

## Core Split

External design collaborator owns:

- visual direction and mood
- composition and layout concepts
- typography and color direction
- interaction feel and polish
- making the surface not ugly

The coding agent owns:

- product context and user workflow
- required states and acceptance criteria
- technical constraints and existing repo patterns
- implementation feasibility
- browser validation, accessibility, responsive behavior, and final QA

## GitHub Issue Model

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

When asking Claude Design, Codex, or another collaborator for a pass,
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
- visual direction: brand cues, references, things to avoid, style freedom, and
  product-specific assets or iconography tied to the product name, domain, and
  purpose rather than generic marks
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
- Finish passes should leave reviewable proof for key states, accessibility
  basics, responsive behavior, and destructive-action safety when relevant.

## Repo Documentation Boundary

Keep durable product and implementation facts in repo docs:

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

1. Think in chat first when direction is fuzzy. Use `github-plan` to search
   existing planning/design issues, then create or update one canonical issue
   when work should persist. For broad redesigns, use a parent and sub-issues
   for independent surfaces, states, implementation tracks, or validation.
2. Put the design request or critique prompt in that issue or a comment.
3. Evaluate returned design against acceptance criteria, constraints, and
   required states using the guidance above.
4. Implement when the user explicitly asks, when accepted direction is already
   captured and execution is the requested next step, or after an explicitly
   requested external design pass has been accepted. When the user has asked to read or prepare from a design brief and then
   explicitly says to implement it, start coding, or move to implementation, proceed
   without another confirmation unless a real blocker or scope ambiguity
   remains. A bare "go" or "go ahead" alone does not approve switching into
   implementation.
5. Use `browser-ui-review` after implementation across relevant interactions
   and viewports; attach or reference browser evidence before signoff.
6. Keep `Decisions`, `Acceptance Criteria`, `Validation`, and `Current Status`
   current with accepted direction, intentional departures, evidence, PR links,
   and remaining work. `Current Status` is the future-session recovery point.
   Link implementation PRs with `Refs #123` unless auto-close is clearly intended and
   validation can conclusively finish the issue. Before closeout, reconcile
   stale/related issues rather than leaving conclusions only in chat.
