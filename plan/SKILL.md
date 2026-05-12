---
name: plan
description: Legacy local plan files only. Use only when explicitly requested; use github for GitHub-backed planning.
metadata:
  short-description: Manage explicit local/offline plans
---

# Local Plan Override

This skill intentionally overrides the upstream/system `plan` skill for this
skills repo. Local plan files are a legacy/offline escape hatch, not the normal
planning workflow.

## Default Routing

- For GitHub-backed repositories, use the `github` skill for durable planning,
  blockers, milestones, Projects, workstream status, and implementation
  coordination.
- Use local plan files only when the user explicitly asks for a local/offline
  plan, when a repo is not GitHub-backed, or when private context should not be
  recorded in GitHub.
- Do not create or update repo docs as roadmaps, scratch plans, temporary
  handoffs, speculative architecture notes, or planning trackers.
- Repo docs should describe implemented/current behavior, configuration, or
  stable operational policy.
- Memory may store durable principles and pointers to the canonical tracker,
  but not the full plan.

## If Explicitly Asked For A Local Plan

1. Resolve the plans directory as `$CODEX_HOME/plans`, or `~/.codex/plans` when
   `CODEX_HOME` is unset.
2. Read repo context only as needed; do not modify the repository.
3. Draft the plan in chat first unless the user explicitly asked to write it.
4. Save only under the plans directory, with frontmatter containing `name` and
   `description`.
5. Keep the plan concise, implementation-oriented, and easy to migrate into a
   GitHub Issue later.

When the user asks for planning in a GitHub-backed repo without explicitly
requesting a local/offline plan file, stop and use `github` instead.
