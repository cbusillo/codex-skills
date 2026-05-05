---
name: plan
description: Use only when the user explicitly asks for a local/offline plan file, wants to find/read/update/delete existing files in $CODEX_HOME/plans (default ~/.codex/plans), or says not to use GitHub. For ordinary durable planning, GitHub issue-backed planning, workstreams, roadmaps, milestones, labels, Projects, or cross-repo blockers, use the github-plan skill instead.
metadata:
  short-description: Manage local/offline plan files
---

# Plan

## Overview

Implicit invocation is disabled for this legacy local plan skill. Prefer
`github-plan` for normal planning. Use this skill only when explicitly invoked
or when maintaining existing local/offline plan files.

Default planning has moved to the `github-plan` skill. Use this local
file-backed skill only for explicitly offline/private scratch plans or for
maintaining existing files in `$CODEX_HOME/plans`.

Draft structured plans that clarify intent, scope, requirements, action items, testing/validation, and risks.

Optionally, save plans to disk as markdown files with YAML frontmatter and free-form content. Saved plans are working memory and should live outside repositories by default so they can evolve without PR churn or accidental exposure of semi-sensitive context. When drafting in chat, output only the plan body without frontmatter; add frontmatter only when saving to disk. Only write to the plans folder; do not modify the repository codebase.

This skill can also be used to draft codebase or system overviews.

## Core rules

- Resolve the plans directory as `$CODEX_HOME/plans` or `~/.codex/plans` when `CODEX_HOME` is not set.
- Create the plans directory if it does not exist.
- Never write to the repo; only read files to understand context.
- Require frontmatter with **only** `name` and `description` (single-line values) for on-disk plans.
- When presenting a draft plan in chat, omit frontmatter and start at `# Plan`.
- Enforce naming rules: short, lower-case, hyphen-delimited, prefixed with the repo/workspace handle; filename must equal `<name>.md`.
- If a plan is not found, state it clearly and offer to create one.
- Allow overview-style plans that document flows, architecture, or context without a work checklist.
- Keep active plans updated as work proceeds. Finished plans should be removed once their useful state has been captured or is no longer needed.
- Treat stale plans as migration candidates: fold useful context into an active long-term or medium-term plan, then remove the stale file.

## Plan tiers

Use tiers to keep planning useful without creating a flat pile of documents:

- **Long-term plans**: durable repo/product direction. Usually keep one active
  long-term plan per repo or product, sometimes two when there are genuinely
  separate strategic tracks.
- **Medium-term plans**: current workstream, phase, or project slice. These are
  tactical and should be updated frequently during implementation.
- **Short-term checklists**: immediate turn/session work. Keep these inside the
  medium-term plan or in chat unless continuity across sessions is needed.

Plans are working memory, not an archive. Durable product decisions belong in
repo docs when approved; completed working plans should be deleted.

## Plan naming

Saved plan names must start with the repo, product, or workspace handle so the
plans directory is easy to scan:

```text
<repo-or-workspace>-<topic>.md
```

Examples:

```text
launchplane-product-direction.md
launchplane-service-foundation.md
mediaforce-encode-recovery-loop.md
workspace-codex-skills-refresh.md
odoo-ai-testing-gate-refresh.md
local-machine-proxmox-maintenance.md
```

For cross-repo work, use a clear owner handle such as `workspace`, `odoo`,
`local-machine`, or the repo that owns the decision. If an existing active plan
has a less strict historical name, do not rename it without user approval; use
the naming rule for new plans.

## Decide the task

1. **Find/list**: discover plans by frontmatter summary; confirm if multiple matches exist.
2. **Read/use**: validate frontmatter; present summary and full contents.
3. **Create**: inspect repo read-only; choose tier and style (long-term, medium-term, implementation, or overview); draft plan; write to plans directory only.
4. **Update**: load plan; revise status, checklist items, current facts, and/or description; preserve frontmatter keys; overwrite the plan file.
5. **Migrate stale**: read stale plans, move still-useful context into the active plan, then remove stale files with user approval when ambiguity exists.
6. **Delete**: remove finished or obsolete plan files when the work is complete or the useful context has been migrated. Confirm only if the user's intent is unclear.

## Plan discovery

- Prefer `scripts/list_plans.py` for quick summaries.
- Use `scripts/read_plan_frontmatter.py` to validate a specific plan.
- If name mismatches filename or frontmatter is missing fields, call it out and ask whether to fix.
- Before creating a new plan, search for existing plans with the same repo/workspace prefix and related topic. Prefer updating an active plan over creating a near-duplicate.

## Plan creation workflow

1. Scan context quickly: read README.md and obvious docs (docs/, CONTRIBUTING.md, ARCHITECTURE.md); skim likely touched files; identify constraints (language, frameworks, CI/test commands, deployment).
2. Ask follow-ups only if blocked: at most 1-2 questions, prefer multiple-choice. If unsure but not blocked, state assumptions and proceed.
3. Identify whether the plan is long-term direction, medium-term workstream, or short-term checklist. Use medium-term by default for active implementation work.
4. Identify scope, constraints, and data model/API implications (or capture existing behavior for an overview).
   If the work changes repo workflow metadata such as docs routing, validation
   gates, primary commands, important workflows, health endpoints, cleanup
   policy, repo relationships, or ownership boundaries, include an action item
   to review `.github/github-repo-workflow.json`.
5. Draft either an ordered implementation plan or a structured overview plan with diagrams/notes as needed.
6. Use a repo/workspace-prefixed slug if saving.
7. Immediately output the plan body only (no frontmatter), then ask the user if they want to 1. Make changes, 2. Implement it, 3. Save it as per plan.
8. If the user wants to save it, prepend frontmatter and save the plan under the computed plans directory using `scripts/create_plan.py`.


## Plan update workflow

- Re-read the plan and related code/docs before updating.
- Keep the plan name stable unless the user explicitly wants a rename.
- If renaming, update both frontmatter `name` and filename together.
- Update plans as work proceeds: mark completed checklist items, add newly discovered constraints, record blockers, and remove or rewrite stale assumptions.
- Keep workflow metadata follow-up visible until it is resolved, intentionally
  deferred, or captured in repo docs/`.github/github-repo-workflow.json`.
- When finishing a workstream, either delete the completed medium-term plan or migrate any durable decisions into repo docs or the relevant long-term plan, then delete the completed plan.
- When several stale plans overlap, consolidate useful context into the active long-term or medium-term plan and remove the stale files rather than leaving archival clutter.

## Scripts (low-freedom helpers)

Create a plan file (body only; frontmatter is written for you). Run from the plan skill directory:

```bash
python ./scripts/create_plan.py \
  --name codex-rate-limit-overview \
  --description "Scope and update plan for Codex rate limiting" \
  --body-file /tmp/plan-body.md
```

Read frontmatter summary for a plan (run from the plan skill directory):

```bash
python ./scripts/read_plan_frontmatter.py ~/.codex/plans/codex-rate-limit-overview.md
```

List plan summaries (optional filter; run from the plan skill directory):

```bash
python ./scripts/list_plans.py --query "rate limit"
```

## Plan file format

When drafting in chat, output only the body with no frontmatter. When saving,
prepend frontmatter with only `name` and `description`.

Read `references/plan-templates.md` only when you need exact implementation or
overview plan templates.

## Writing guidance

- Start with 1 short paragraph describing intent and approach.
- Keep action items ordered and atomic (discovery -> changes -> tests -> rollout); use verb-first phrasing.
- Scale action item count to complexity (simple: 1-2; complex: up to about 10).
- Include file/entry-point hints and concrete validation steps where useful.
- Always include testing/validation and risks/edge cases in implementation plans; include safe rollout/rollback when relevant.
- Use open questions only when necessary (max 3).
- Avoid vague steps, micro-steps, and code snippets; keep the plan implementation-agnostic.
- For overview plans, keep action items minimal and set non-applicable sections to "None."
