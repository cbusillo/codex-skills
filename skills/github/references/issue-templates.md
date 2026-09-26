# GitHub Plan Issue Templates

Read before creating or editing a planning issue, including adoption of a
contributor request. Use the structure below for a concrete finish line and
enough recovery context for the next session.

For every human-authored issue, preserve the original title and request
verbatim, regardless of the author's repository association. Repository owner,
member, and collaborator roles grant permissions but never grant automation
ownership of the author's words. Prefer a bot-authored comment or a linked
automation-authored planning issue. When the plan must live in the human issue
body, use this ownership boundary:

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

Subsequent automation updates may change only the managed block. The managed
provenance marker by itself never transfers human-authored content to
automation ownership. Duplicate,
missing, or out-of-order markers must fail closed rather than rewriting the
issue. Maintainer-owned and bot-authored planning issues may use the fully
managed format below. An older contributor issue that already mixes unmarked
planning headings with request text is ambiguous and must not be migrated by
guessing; preserve its request in a linked maintainer-owned plan or perform an
explicit, reviewed migration from issue history.

`show` may read contributor-owned planning sections without granting write
ownership. Inspect its provenance: `automation_managed`, `contributor_envelope`,
or `contributor_unmanaged`. `section_updates_allowed: false` leaves the current
body read-only. A plain request without unmarked planning headings or reserved
markers may be adopted through `update-section`'s preservation envelope; do not
rewrite the source request yourself.

After adoption, section updates use only the managed block. Unmarked recognized
planning headings are ambiguous and must fail closed. Section content must not
contain reserved ownership markers. With malformed markers, `show --full`
still returns the raw body with ownership `unknown`; section parsing and all
body writes remain fail-closed. Native relationship updates do not rewrite the
body and therefore do not require body ownership.

Fully managed plans created by `gh-plan` begin with:

```markdown
<!-- github-plan:managed-provenance -->
```

Full-body management requires authorship by the acting planning bot or an
owner-controlled login in `CODEX_AUTOMATION_BOT_LOGINS`; unknown authors and
authors outside that configured set stay in preservation mode. This marker
records provenance but does not transfer ownership of a human-authored body.
The generic
`<!-- github-skill-operation:... -->` comment is only request reconciliation
evidence and never grants body ownership. Existing contributor envelopes always
remain contributor-owned.

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

## Waiting On An External Gate

When another maintainer must create or identify a prerequisite, use an owned
Current Status section or an authorized bot-authored planning comment. Record:

- the gate maintainer, downstream coordinator, return thread, and affected issues
- an explicit request to reply/tag the coordinator there with canonical gate
  links and completion criteria
- who will verify the returned gate and add or reconcile native blockers

Until the gate exists, use `Waiting for:` and `Blocked by: No native issue
blocker; waiting for ...`. Milestone membership or a mention is not a blocker
edge. Do not claim a gate or link exists before verifying it. Once known, link
it under existing authority without requiring another return round-trip.

Example: "Please identify the prerequisite and reply to the coordinator here
with its canonical link and completion criteria. The coordinator will verify
it and reconcile blockers on the affected issues. Until then, this plan waits
for gate identification and linkage."

This record grants no messaging, mention, write, or monitoring authority. If
posting is not authorized, prepare the draft and name the remaining action.
