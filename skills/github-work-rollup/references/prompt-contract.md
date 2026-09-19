# GitHub Work Brief Prompt Contract

Use this contract after collecting GitHub work evidence and any relevant durable
plan state. The agent writes the brief; code collects facts and verifies
grounding.

## Synthesis Prompt

Create the most useful GitHub work brief for this reader and purpose. For an
owner or executive, write it as a human conversation brief whose raw material
happens to be GitHub evidence, not as a GitHub status report. Optimize for
helping the reader know what to ask about, react to, redirect, trust, sequence,
pause, or ask us to prove next.

Use the provided evidence JSON and plan context as the factual boundary. Do not
invent status, intent, ownership, causality, or counts. Mark uncertain
interpretation as inference.

Use these as guidance signals, not as a checklist or section template:

- What materially changed?
- How does it affect the active plan, finish line, or current workstream
  direction?
- What matters next, and who needs to decide or act?
- What risks, blockers, or confidence limits changed?
- What is the recommended next action?
- What choice, correction, or quality bar would most improve the work if the
  reader gave guidance now?

Prefer concise, audience-appropriate prose over exhaustive lists. Group by
meaning and decision value, not by raw GitHub event order. Include links only
when they help the reader inspect, approve, unblock, or verify the work.

Prefer human headings that match the situation. Skip boilerplate sections when
they do not help the reader. Do not include generic closers, offers for a deeper
dive, or empty "no action required" language.

The first screen should answer what guidance would matter. If the evidence does
not support a useful steering move, say what is knowable and name the missing
signal instead of padding the brief.

## Grounding Rules

- Treat evidence JSON as the source of truth for links, issue and PR numbers,
  workflow runs, releases, counts, source notes, and collection windows.
- Treat durable plan issues as the source of truth for plan direction,
  blockers, finish lines, and next actions.
- Say "no plan signal was available" when the evidence does not include plan
  context.
- Reflect every evidence source note or limitation as an audience-shaped caveat.
  Fold limitations into the relevant judgment. Do not create a standalone
  confidence or limits section unless uncertainty is itself the main decision.
  Group repetitive notes by decision impact instead of transcribing collector
  output verbatim.
- Label static backlog or inventory counts at first mention. Do not imply they
  are window deltas unless the evidence includes opened, closed, moved, or
  changed-in-window facts.
- Anchor trajectory and trend claims to observed window movement. If only a
  snapshot is available, say that direction cannot be inferred from the evidence.
- Use configured priority-section metadata when present. Preserve the difference
  between `portfolio_area`, `workstream`, `relationship`, and `initiatives` so a
  broad GitHub grouping does not get mistaken for the specific workstream.
- Treat `derived_context` as grounded explanatory context with provenance, not
  as a manual source of truth. Use it to translate repositories and workstreams
  into human meaning, while preserving confidence and staleness. Do not present
  standing repository descriptions as changes inside the report window, and do
  not infer product strategy beyond the collected context.
- Distinguish observed facts from inference and recommendation when the reader
  might act on the difference.
- Do not use fixed report templates, renderer modes, canned report examples, or
  generic bucket dumps.
- Do not use the synthesis questions as headings or as a visible checklist.
  Avoid report-template headings such as "Confidence And Limits",
  "Recommended Default", "Next Steps & Decisions", "Delivery Cadence", and
  "Impact & Confidence" unless they are the reader's own terms. Prefer
  situational headings such as "Where Justin's Guidance Would Matter", "What To
  Keep Moving", "What Looks Over-Invested", or "What Needs A Business Call".
- Do not lead with velocity math, item counts, or percentage changes unless the
  volume itself is the decision. Translate activity into outcome, risk,
  sequencing, or guidance value first; put counts in supporting evidence.
- Do not turn weak signals into generic management advice. If the evidence does
  not show a real decision, say what is known and keep the recommendation small.
- Use GitHub items as evidence labels, not as the main topic. Prefer portfolio
  areas, workstreams, initiatives, customer impact, risk, sequencing, and
  guidance leverage. Name repositories, PRs, workflow runs, branches, and
  internal components only when the reader needs to inspect, approve, unblock,
  or judge a specific item.

## Audience Dial

Audience changes altitude and emphasis, not the factual boundary.

- Peer/operator: queue movement, exact blockers, owners, links, and next steps.
- Manager: focus, sequencing, risk, confidence, decisions, plan fit, and the
  smallest useful guidance request.
- Executive/customer: bottom line, trajectory, confidence, recommendation, and
  minimal implementation detail. Name workstreams by product, customer impact,
  business outcome, or decision; do not enumerate repositories, PRs, internal
  components, or infrastructure mechanisms unless one is itself the decision.
  If the brief is for a product/workflow leader, emphasize how the work changes
  their ability to guide, trust, ship, or stop the work. Avoid
  engineering-manager filler such as velocity, CI health, delivery cadence, or
  component progress unless it changes a product, cost, trust, customer, or
  staff-time decision.
- Owner conversation brief: tell the human story of what the team built or
  changed, why it matters, and what the reader can talk about with the team.
  Prefer concrete handles such as "Codex Lab is becoming the agent harness" over
  status phrasing such as "the Codex Lab workstream has active items." Use a
  light spine, not a rigid template: short version, things worth talking about,
  where guidance would help, and receipts when those sections are useful.

When evidence was collected in standup or operator mode but the reader is a
manager or executive, translate open backlog into what is not done and why it
matters, translate completed items into what the team delivered and what it
enables, and omit counts unless volume itself is the decision.

A recommendation for a manager or executive is a decision, question, or priority
call, not a next-task instruction. Phrase it as the decision the reader can
improve.

When the reader is unclear, choose the lowest-friction useful default for the
request and state the assumption. Ask only when the missing audience or decision
context would materially change the brief.

## Failure Shape

If collection failed or the evidence is too thin, do not pad. State what was
attempted, what evidence is missing, what can still be concluded, and the next
collection or planning action that would make the brief reliable.

If the evidence is too narrow to tell the reader something they could not
already infer, say so plainly. A short brief is better than a padded one.
