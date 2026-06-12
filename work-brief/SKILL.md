---
name: work-brief
description: Synthesize collected work evidence and durable plan context into audience-appropriate briefs. Use when the user asks for a status update, daily or weekly report, standup summary, manager or executive update, handoff narrative, what changed, where work stands, what matters next, or how recent work fits the plan. This skill reports and recommends from evidence; it does not mutate GitHub state or durable plans.
metadata:
  short-description: Write plan-anchored work briefs from evidence
resources:
  - path: references/prompt-contract.md
    kind: reference
    description: Prompt contract for evidence-grounded work brief synthesis.
  - path: scripts/verify_work_brief.py
    kind: script
    description: Deterministic checker for unsupported links, issue refs, plan refs, and source notes.
commands:
  - name: verify-work-brief
    source: skill
    resource_path: scripts/verify_work_brief.py
    example_argv:
      ["uv", "run", "scripts/verify_work_brief.py", "--evidence", "evidence.json", "--brief", "brief.md"]
    purpose: Verify that a generated brief stays grounded in collected evidence and optional plan context.
workflow_defaults:
  - name: window
    value: 24h
    description: Default lookback when the user does not specify a reporting window.
---

# Work Brief

## Purpose

Use this skill when the deliverable is a readable brief: a status update,
standup, daily or weekly report, leadership update, manager update, handoff, or
answer to "what changed," "where are we," "what matters next," or "how does
this fit the plan?"

This skill turns evidence into judgment. Code collects facts and checks the
result; the agent synthesizes meaning, plan fit, risk, confidence, and next
action for the reader.

## Boundaries

- Use `github` for GitHub operations, issue writes, PR comments, PR creation,
  merges, branch cleanup, and CI diagnostics.
- Use `github-plan` when the user wants to create, update, reconcile, or query
  durable plan issues, parent/sub-issue graphs, blockers, or Project fields.
- Use `babysit-pr` when one PR needs continuous CI and review monitoring.
- Use `repo-readiness` when the question is whether a branch, PR, or workstream
  is ready to review, merge, ship, pause, or hand off.
- Use `work-closeout` when the user wants safe-to-exit hygiene, stale plan
  cleanup, branch cleanup, artifact cleanup, or final closeout.

This skill may recommend those handoffs. It must not perform their write actions
unless the user separately asks for them and the owning skill is invoked.

## Workflow

1. Resolve the reader, purpose, scope, and window from the request. If the brief
   would be misleading without the reader or decision context, ask the smallest
   useful question; otherwise state the assumption and proceed.
2. Collect facts from the owning evidence source. For GitHub work, prefer the
   `github-work-evidence` command from the `github` skill and keep its JSON as
   the factual boundary.
3. Pull durable plan context when the user asks about direction, drift, roadmap,
   priorities, blockers, or "what's next." Use `github-plan` for plan issue
   state; do not infer plan direction from activity volume alone.
4. Synthesize the brief using `references/prompt-contract.md`. Let the reader's
   decision shape the altitude and structure. Do not use a rigid template, and
   do not list every event unless the user asks for a queue.
5. Verify the draft with `scripts/verify_work_brief.py` against the evidence
   JSON. If the brief cites durable plan issues, pass the plan JSON with
   `--plan-context`. Revise until unsupported links, unsupported issue
   references, and missing source notes are resolved.
6. Present the brief with clear assumptions and limitations. If the evidence is
   thin or degraded, say what can be concluded and what cannot.

## Synthesis Rules

- Start with the most decision-useful truth for this reader.
- Explain what materially changed, not merely what happened.
- Say how the work fits the active plan: toward the finish line, sideways, or
  drift. If no plan signal is available, say so directly.
- Make the next action or decision explicit.
- Separate facts, inference, and recommendation when uncertainty matters.
- Use links only when they support inspection, approval, or follow-up.
- Reflect every source note or collection limitation from the evidence.
- Keep counts and volume language tied to evidence counts, not intuition.
- Do not use fixed report templates, renderer modes, or canned report examples.

## Audience Handling

Audience is context, not a renderer switch. The same evidence should be shaped
for the reader's decision:

- A peer or operator needs concrete queue movement, blockers, owners, and links.
- A manager needs focus, sequencing, plan fit, risks, decisions, and confidence.
- A leadership or customer reader needs bottom line, trajectory, risk, and the
  recommended decision with minimal implementation detail.

If local private people context is available through the `people` skill, it can
inform tone, depth, and risk lens. Do not publish private people notes.
