---
name: memory-distillation
description: Use only when the user explicitly asks to audit, clean, prune, archive, reset, or distill Codex memories into skills, repo docs/issues, or local config. Never use implicitly or for ordinary repo work.
metadata:
  short-description: Audit memories into durable sources
---

# Memory Distillation

Use this skill only when the user explicitly invokes it or clearly asks to audit,
clean, prune, archive, reset, or distill memories.

## Hard Gates

- Do not use this skill implicitly. If the user did not explicitly ask for memory
  distillation, do not load or apply this workflow.
- Start in read-only mode: inventory, audit, classify, and propose changes first.
- Do not edit, delete, move, archive, or generate memory files, skills, repo docs,
  issues, or local config until the user explicitly approves the proposed action.
- Treat approval as scoped. Approval for one memory cleanup does not authorize a
  different cleanup, repo edit, issue update, or skill change.
- Never put secrets, credentials, private hostnames, customer/client data, private
  message contents, local-only paths, machine-specific values, or personal account
  details into public skills.

## Source Roles

- **Memories**: short-term observations and candidate facts. They are not
  authoritative when they conflict with skills, repo docs, code, current GitHub
  state, or local config contracts.
- **Skills**: public, durable agent behavior and reusable workflows.
- **Repo docs / GitHub issues**: repo-specific product, project, architecture,
  roadmap, and follow-up truth.
- **Local config**: private, machine-specific, account-specific, or environment
  details. Prefer gitignored overlays for anything not safe to publish.

## Audit Workflow

1. Inventory memory sources relevant to the request. Prefer canonical memory and
   recent summaries before broad historical archives.
2. Search for stale workflow claims, private details, time-sensitive facts,
   command habits, issue/PR state, and facts that duplicate maintained sources.
3. Classify each candidate as one of:
   - `promote-to-skill`
   - `promote-to-repo-doc-or-issue`
   - `move-to-local-config`
   - `keep-historical`
   - `delete-or-archive`
4. Verify promotable facts against maintained sources before proposing them.
   Examples: local code, skill files, repo docs, GitHub state, official docs, or
   config schemas.
5. Present a concise proposal with exact files or memory entries affected, the
   classification, and the reason.
6. Ask for explicit human approval before making any changes.

## Promotion Rules

- Promote to a skill only when the fact is reusable, public-safe, durable, and
  procedural.
- Promote to repo docs or GitHub issues when the fact is repo-specific, product
  specific, architectural, or project-planning related.
- Move to local config only when the fact is private/local and has a clear config
  contract or schema. If no contract exists, propose one before writing data.
- Keep historical memories only when they are useful for later investigation and
  clearly non-authoritative.
- Delete or archive memories that are stale, misleading, private, duplicative, or
  likely to bias agents toward obsolete behavior.

## Conflict Rules

- Current maintained sources beat memory.
- Helper contracts beat recalled command habits.
- Skills beat old Chronicle observations for agent workflow behavior.
- Repo docs and GitHub issues beat memory for project state.
- Local config beats skills for private/local values, but not for public workflow
  policy.

## Reporting

Keep reports practical:

- What memory source was audited.
- What should be promoted, moved, kept, archived, or deleted.
- What evidence supports each recommendation.
- What changes need explicit approval.
- What remains unknown or risky.
