---
name: github-work-rollup
description: Produce read-only GitHub work rollups across configurable repositories, owners, subjects, labels, and time windows. Use when the user asks what is active, blocked, waiting, recently completed, needs attention, or changed recently across GitHub work, including daily reports, activity summaries, standup briefs, or configurable work digests.
metadata:
  short-description: Roll up current GitHub work
resources:
  - path: scripts/github_work_rollup.py
    kind: script
    description: Read-only GitHub work collector and Markdown/JSON renderer.
  - path: references/github-work-rollup.local.example.yaml
    kind: reference
    description: Public-safe example local config for routine rollup defaults.
commands:
  - name: github-work-rollup
    source: skill
    resource_path: scripts/github_work_rollup.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/github_work_rollup.py",
        "--repo",
        "example-org/example-repo",
        "--window",
        "24h",
        "--format",
        "markdown",
      ]
    purpose: Emit a read-only GitHub work rollup for configured or requested repos.
workflow_defaults:
  - name: window
    value: 24h
    description: Default lookback when no local config or user override provides one.
  - name: config
    value: .local/github-work-rollup.yaml
    description: Optional ignored local defaults for routine subjects, repos, filters, and output.
---

# GitHub Work Rollup

## Purpose

Use this skill for read-only situational awareness across GitHub work: what is
active, blocked, waiting, ready for review, ready for a merge decision, stale, or
recently completed. It is the radar screen for GitHub work, not the workflow that
acts on the radar blips.

## Boundaries

- Use `github` for GitHub operations such as PR creation, comments, merges,
  checks diagnostics, branch cleanup, and issue writes.
- Use `github-plan` for durable planning state, parent/sub-issue graphs,
  blockers, Project fields, and plan issue reconciliation.
- Use `babysit-pr` when one PR needs continuous monitoring, CI retries, review
  feedback handling, or push/fix/watch loops.
- Use `repo-readiness` when the main question is whether a change, branch, PR,
  or workstream is ready to review, merge, ship, pause, or hand off.
- Use `work-closeout` for safe-to-exit hygiene, local artifact cleanup, branch
  cleanup, handoff migration, and final closeout summaries.

This skill may recommend one of those handoffs. It must not perform their write
actions in v1.

Implicit invocation is safe because v1 is read-only, preflights GitHub access,
and emits rollup reports only; any GitHub write must be handed off to another
skill or explicitly requested by the user.

## Inputs

Inputs can come from explicit user instructions, CLI flags, repo metadata, or an
ignored local config file. User instructions override local config. Local config
overrides defaults.

Supported config fields:

- `timezone`
- `default_window`
- `report_recipient`
- `subjects`
- `repo_owners`
- `repositories`
- `summary_level`: `concise`, `standard`, or `detailed`
- `mode`: `activity`, `backlog`, or `standup`
- `layout`: `operator`, `manager`, or `executive`
- `output_path`
- `include_external_activity`
- `include_bots`
- `noise_filters`
- `priority_sections`

Use `.local/github-work-rollup.yaml` for private routine defaults. Do not commit
private subjects, repository lists, output paths, or personal routing details.
Use `references/github-work-rollup.local.example.yaml` as the public-safe shape.
If the local config file is absent, continue with explicit user scope and built-in
defaults.

Modes:

- `activity` is the default recent-activity digest. It applies the window to
  open and completed work, so older open backlog is intentionally omitted.
- `backlog` includes open work regardless of update time and keeps completed
  work window-bound.
- `standup` combines open backlog with recent activity and completions. Use it
  for questions like "what is next," "what are we blocked on," and routine
  working-session briefs.

Repository open-work collection follows the selected mode. Subject search stays
window-bound in all modes so broad author/commenter/mention scans remain a
recent activity signal rather than an unbounded people search.

Layouts:

- `operator` is the detailed work queue for the person doing the work. It keeps
  concrete issues, PRs, buckets, source lanes, links, and handoff guidance.
- `manager` is the daily planning brief. It emphasizes priorities, active work,
  focus areas, decisions, risks, velocity, and source notes.
- `executive` is the leadership brief. It should be readable in under five
  minutes, target one page on normal days and no more than two pages on heavy
  days, start with outcomes and meaning, mention Every Code and skills impact
  where relevant, and keep GitHub counts as supporting evidence.

`summary_level` controls verbosity inside the selected layout. It is not an
audience selector. `mode` controls what data is collected; `layout` controls who
the report is for.

## Workflow

1. Resolve scope from the request and optional local config: repositories,
   owners, subjects, mode, time window, timezone, output format, and summary
   level. Prefer `--mode standup` when the user asks for active work or next
   work. Prefer `--mode activity` when they ask what changed recently.
2. Run the helper in read-only mode:

   ```bash
   uv run scripts/github_work_rollup.py \
     --repo example-org/example-repo \
     --mode standup \
     --window 24h \
     --format markdown
   ```

   For a planning or executive daily brief, choose the audience layout explicitly:

   ```bash
   uv run scripts/github_work_rollup.py \
      --repo example-org/example-repo \
      --mode standup \
      --report-recipient "Example leader" \
      --window 24h \
      --layout executive \
     --format markdown
   ```

   Use `operator` for the concrete queue, `manager` for daily planning, and
   `executive` for a daily conversation overview. Executive output should target
   one page on normal days and two pages on heavy days. It should explain what
   changed, why it matters, how Every Code and skills are affected, risks or
   decisions, and compact velocity counts. It should not enumerate PRs and
   issues except when a link is useful for action or verification.

3. If routine local defaults are needed, pass the private config explicitly or
   let the helper read `.local/github-work-rollup.yaml` when it exists:

   ```bash
   uv run scripts/github_work_rollup.py \
     --config .local/github-work-rollup.yaml \
     --format json
   ```

4. Treat the helper output as the source of truth for collected GitHub state. It
   includes collection metadata, auth/API preflight status, rollup buckets, and
   limitations.
5. Write a concise judgment-oriented report. Do not dump every event. Emphasize:
   - needs attention
   - blocked or waiting work
   - ready for review
   - ready for merge decision
   - in progress
   - stale or needs reconciliation
   - recently completed
   - configured priority sections such as skill updates
6. Include links, issue/PR numbers, run IDs, and numeric identifiers only when
   the reader should inspect, comment, approve, unblock, or follow up.
7. If the rollup identifies action, recommend the owning skill instead of acting:
   - `babysit-pr` for one PR needing watch/fix/retry handling
   - `repo-readiness` for detailed gates before a merge/ship decision
   - `github-plan` for stale or inconsistent planning state
   - `work-closeout` for finished workstreams that need cleanup or parking
   - `github` for explicit GitHub writes after user approval

## Failure Handling

The helper preflights GitHub access before collecting. If auth or API access is
unhealthy, fail fast and emit a fresh failure report instead of stale rollup
content. The report should include attempted timestamp, failed command, relevant
stdout/stderr excerpt, likely cause, and the next command or permission change to
try.

## Non-Goals

- No GitHub mutations in v1.
- No Project, issue relationship, label, branch, or comment writes.
- No CI retries or PR babysitting loop.
- No readiness gate execution.
- No cleanup, safe-to-exit claims, or handoff migration.
- No long-term analytics store or dashboard.

## Output Style

Default to compact Markdown for humans and JSON for downstream automation when
requested. Keep the first section useful even if the reader stops there. Name the
time window and sources. Mention limitations explicitly when GitHub data,
Project fields, Launchplane context, or configured metadata is unavailable.
