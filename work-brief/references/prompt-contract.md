# Work Brief Prompt Contract

Use this contract after collecting evidence and any relevant durable plan state.
The agent writes the brief; code only collects facts and verifies grounding.

## Synthesis Prompt

Create the most useful work brief for this reader and purpose.

Use the provided evidence JSON and plan context as the factual boundary. Do not
invent status, intent, ownership, causality, or counts. Mark uncertain
interpretation as inference.

Answer these questions in whatever structure best serves the reader:

- What materially changed?
- How does it affect the active plan, finish line, or current workstream
  direction?
- What matters next, and who needs to decide or act?
- What risks, blockers, or confidence limits changed?
- What is the recommended next action?

Prefer concise, audience-appropriate prose over exhaustive lists. Group by
meaning and decision value, not by raw GitHub event order. Include links only
when they help the reader inspect, approve, unblock, or verify the work.

## Grounding Rules

- Treat evidence JSON as the source of truth for links, issue and PR numbers,
  workflow runs, releases, counts, source notes, and collection windows.
- Treat durable plan issues as the source of truth for plan direction,
  blockers, finish lines, and next actions.
- Say "no plan signal was available" when the evidence does not include plan
  context.
- Reflect every evidence source note or limitation as a confidence caveat.
- Distinguish observed facts from inference and recommendation when the reader
  might act on the difference.
- Do not use fixed report templates, renderer modes, canned report examples, or
  generic bucket dumps.

## Audience Dial

Audience changes altitude and emphasis, not the factual boundary.

- Peer/operator: queue movement, exact blockers, owners, links, and next steps.
- Manager: focus, sequencing, risk, confidence, decisions, and plan fit.
- Executive/customer: bottom line, trajectory, confidence, recommendation, and
  minimal implementation detail.

When the reader is unclear, choose the lowest-friction useful default for the
request and state the assumption. Ask only when the missing audience or decision
context would materially change the brief.

## Failure Shape

If collection failed or the evidence is too thin, do not pad. State what was
attempted, what evidence is missing, what can still be concluded, and the next
collection or planning action that would make the brief reliable.
