# GitHub Plan Issue Templates

Durable planning issues should follow a consistent structure to ensure they
are easily scannable and contain the necessary context for agents and humans.

For issues authored by contributors outside the repository owner/member/
collaborator set, preserve the original request verbatim. Prefer a bot-authored
comment or a linked maintainer-owned planning issue. When the plan must live in
the contributor issue body, use this ownership boundary:

```markdown
## Original request

<!-- github-plan:original-request:start -->
[Original body verbatim, or the original title when the body was empty]
<!-- github-plan:original-request:end -->

---

_The following implementation plan is maintained by project automation._

<!-- github-plan:managed:start -->
[Required planning headings]
<!-- github-plan:managed:end -->
```

Subsequent automation updates may change only the managed block. Duplicate,
missing, or out-of-order markers must fail closed rather than rewriting the
issue. Maintainer-owned and bot-authored planning issues may use the fully
managed format below. An older contributor issue that already mixes unmarked
planning headings with request text is ambiguous and must not be migrated by
guessing; preserve its request in a linked maintainer-owned plan or perform an
explicit, reviewed migration from issue history.

`gh-plan show`, relationship updates, and section updates read only the managed
block after adoption. Any unmarked recognized planning heading is ambiguous and
must fail closed. Section content must not contain the reserved ownership
markers.

Fully managed plans created by `gh-plan` begin with:

```markdown
<!-- github-plan:managed-provenance -->
```

This marker establishes plan-body provenance for member/collaborator-authored
issues. The generic `<!-- github-skill-operation:... -->` comment is only
request reconciliation evidence and never grants body ownership. The acting
planning bot and repository owner remain fully managed without the provenance
marker. Legacy markerless member/collaborator plans remain managed only when
they retain the exact canonical heading layout; existing contributor envelopes
always remain contributor-owned.

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

### Current Status
The recovery point for future sessions. Keep it short and concrete.

```text
State: [Active/Blocked/Stale/Done]
Next action: [The single next concrete step]
Blocked by: [Reference to other issue or PR]
Last verified: [Date/Commit]
```

### Scope
- **In**: What is being changed.
- **Out**: What is intentionally NOT being changed (important for limiting drift).

### Acceptance Criteria
A checklist of functional and technical requirements.

### Relationships
Native GitHub dependencies and sub-issue links, plus explanatory prose if
needed.

### Validation
Concrete steps or commands used to verify the work.

### Decisions
A log of architectural or product decisions made during the workstream.

### Open Questions
Items that still need clarification or manager approval.
