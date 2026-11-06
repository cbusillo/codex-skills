# GitHub Plan Issue Templates

Read before creating or editing a planning issue. Use the structure below for a concrete finish line and
enough recovery context for the next session.

For a linked automation-authored plan, write the plan with a qualified source
link, then preserve any exact human title/request or quoted source material in
a `gh-comment --verbatim` comment. New plan bodies and section text are authored
planning content and require qualified issue references.

The title and body of an issue the automation identity did not author are the
author's words, whatever their repository association. Planning writers never
edit that body: `update-section` posts the section as a bot-authored
`## <Section>` comment, `claim` and `release` carry Current Status in their own
comments, and a `related` note refuses, leaving native relationships or a
comment. Readers take the newest trusted `## Current Status` comment over older
body status.

`show` reports provenance: `automation_managed`, `contributor_envelope` (an
`## Original request` envelope written by older helpers, still read but never
written), `contributor_unmanaged`, or `unknown` for malformed markers. Only
`automation_managed` has `section_updates_allowed: true`. Section content must
not contain reserved ownership markers, and malformed markers fail closed.
Native relationship updates do not touch the body.

Fully managed plans created by `gh-plan` begin with:

```markdown
<!-- github-plan:managed-provenance -->
```

Full-body management requires authorship by the acting planning bot or a
Director-controlled login in `CODEX_AUTOMATION_BOT_LOGINS`. The marker records
provenance and never transfers ownership of a body someone else authored; the
generic `<!-- github-skill-operation:... -->` comment is only request
reconciliation evidence.

## Required Headings

```markdown
## Objective
## Finish Line
## Current Status
## Scope
## Acceptance Criteria
## Relationships
## Validation
## Decisions
## Open Questions
```

## Section Definitions

### Objective
A 1-2 paragraph description of the goal, the "why", and the intended approach.

### Finish Line
A compact, observable "Done" state. This should be a specific condition that
can be verified (e.g., "The CLI prints the correct version and all tests pass").
For implementation work, scope this to one PR-sized deliverable using
[issue closing and post-merge proof](repo-workflow.md#issue-closing-and-post-merge-proof);
close it at the PR completing its work. Record a real event finish line as a
wait naming the event and actor; do not create an agent come-back check on a
parent. Preserve the original request when splitting an older broad issue.

### Current Status
The recovery point for future sessions. Keep it short and concrete.

```text
State: [Active/Blocked/Waiting/Stale/Done]
Next action: [The single next concrete step]
Blocked by: [Reference to other issue or PR]
Waiting for: [Named person, decision, or event when applicable]
Last verified: [Date/Commit]
```

### Scope
- **In**: What is being changed.
- **Out**: What is intentionally NOT being changed (important for limiting drift).

### Acceptance Criteria
Plain bullets of functional and technical requirements, not checkboxes; the
issue closes on evidence. A requirement that needs its own work is a native
sub-issue.

### Relationships
Native GitHub dependencies and sub-issue links, plus explanatory prose if
needed.

### Validation
Concrete steps or commands used to verify the work.

### Decisions
A log of architectural or product decisions made during the workstream.

### Open Questions
Items that still need clarification or manager approval.

## Waiting On An External Gate

When another maintainer must create or identify a prerequisite, use an owned
Current Status section or an authorized bot-authored planning comment. Record:

- the gate maintainer, downstream coordinator, return thread, and affected issues
- an explicit request to reply/tag the coordinator there with canonical gate
  links and completion criteria
- who will verify the returned gate and add or reconcile native blockers

Use [Derived Status](../../github-plan/SKILL.md#derived-status) for the waiting
record and dependency linkage. Do not claim a gate or link exists before
verifying it. Once known, link it under existing authority without requiring
another return round-trip.

Example: "Please identify the prerequisite and reply to the coordinator here
with its canonical link and completion criteria. The coordinator will verify
it and reconcile blockers on the affected issues. Until then, this plan waits
for gate identification and linkage."

This record grants no messaging, mention, write, or monitoring authority. If
posting is not authorized, prepare the draft and name the remaining action.
