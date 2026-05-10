# GitHub Plan Issue Templates

Durable planning issues should follow a consistent structure to ensure they
are easily scannable and contain the necessary context for agents and humans.

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
