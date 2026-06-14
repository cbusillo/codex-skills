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
  - path: references/prompt-contract.md
    kind: reference
    description: Agent synthesis prompt and grounding rules for work briefs.
  - path: scripts/verify_work_brief.py
    kind: script
    description: Verifier script that checks a Markdown brief against the evidence JSON.
  - path: scripts/synthesize_work_brief.py
    kind: script
    description: Direct local-LLM work brief synthesizer that uses prompt-contract.md as the system prompt and verifies the result.
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
  - name: synthesize-work-brief
    source: skill
    resource_path: scripts/synthesize_work_brief.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/synthesize_work_brief.py",
        "--evidence",
        "evidence.json",
        "--audience",
        "manager",
        "--brief-output",
        "brief.md",
      ]
    purpose: Generate a verified manager/executive brief from saved evidence through a direct local LLM call.
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
- `people_index`: optional private people index path for recipient tailoring
- `subjects`
- `repo_owners`
- `repositories`
- `summary_level`: `concise`, `standard`, or `detailed`
- `mode`: `activity`, `backlog`, or `standup`
- `layout`: `operator`, `manager`, or `executive`
- `output_path`
- `collection_limit_items`: safety ceiling for PR/issue rows collected per
  repo/state before rendering trims examples
- `release_collection_limit`: safety ceiling for release rows collected per repo
  before window filtering
- `workflow_collection_limit`: safety ceiling for workflow run rows collected per
  repo before window filtering
- `include_external_activity`
- `include_bots`
- `noise_filters`
- `priority_sections`

Each `priority_sections` entry may also include executive-facing metadata:

- `portfolio_area`: broad bucket or product area, such as an internal planning
  section name
- `workstream`: canonical workstream name to render in executive briefs
- `relationship`: plain-language relationship between the workstream and the
  portfolio area
- `initiatives`: compact list of named initiatives inside the workstream

Use these fields when a GitHub grouping label is broader than the work it
contains. For example, a portfolio area can be "Every Code Product Issues" while
the workstream remains "Codex Lab" and the initiative is "Code Bridge". If these
fields are absent, executive rendering infers a workstream from item titles, but
explicit metadata is more reliable.

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

`limit_items` is a display control, not a collection control. Counts and
executive/manager volume language come from the collected rows, then layouts
trim examples for readability. The separate collection-limit fields are safety
ceilings only; if one is reached, the report must include a source note saying
the relevant counts may be incomplete.

Layouts:

- `operator` is the detailed work queue for the person doing the work. It keeps
  concrete issues, PRs, buckets, source lanes, links, and handoff guidance.
- `manager` is the cadence-aware planning brief. It emphasizes priorities,
  active work, focus areas, decisions, risks, velocity, and source notes.
- `executive` is the leadership brief. It should be readable in under five
  minutes, target one page on normal windows and no more than two pages on heavy
  windows, adapt daily/weekly/custom wording to the requested window, start with
  outcomes and meaning, mention Every Code and skills impact where relevant, and
  keep GitHub counts as supporting evidence.

`summary_level` controls verbosity inside the selected layout. It is not an
audience selector. `mode` controls what data is collected; `layout` controls who
the report is for.

When `.local/people.yaml` exists, or a config/CLI `people_index` points to a
private people index, the helper attempts to resolve `report_recipient` against
that index. A matched profile can tailor manager and executive wording toward
the recipient's role, organization, technical depth, framing preference, and
report guidance. Missing or unmatched people data is non-fatal. Do not publish
private people notes; use them only to shape the report.

If the user wants a tailored manager or executive brief and no usable local
people/config context exists, help them provide the missing context before
generating the final report. Ask only for the smallest useful set:

- recipient name and relationship to the work
- organization/product/customer context the recipient cares about
- technical depth preference, such as non-technical, mixed, or technical
- decision/risk lens, such as cost, schedule, prod data, staff time, customer
  impact, reliability, or revenue
- repository scope or owner scope, plus any must-include products or skills

When the user provides that context, use it for the current report and suggest a
private `.local/people.yaml` / `.local/github-work-rollup.yaml` update only if
the same report will be repeated. Do not require private local files for one-off
reports.

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

   For a planning or executive brief, choose the audience layout explicitly:

   ```bash
   uv run scripts/github_work_rollup.py \
      --repo example-org/example-repo \
      --mode standup \
      --report-recipient "Example leader" \
      --people-index .local/people.yaml \
      --window 24h \
      --layout executive \
     --format markdown
   ```

   Use `operator` for the concrete queue, `manager` for planning, and
   `executive` for an owner/leadership conversation overview with a polished,
   outcome-first lead. Executive output should target one page on normal windows
   and two pages on heavy windows. It should explain what changed, why it
   matters, how Every Code and skills are affected, risks or decisions, and
   compact supporting signal. It should not enumerate PRs and issues except when
   a link is useful for action or verification.

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
5. Synthesize a concise judgment-oriented report. Follow the synthesis and
   grounding rules in `references/prompt-contract.md`. When drafting a
   manager, executive, or other narrative brief from JSON evidence, verify the
   brief before presenting it:

   ```bash
   uv run scripts/verify_work_brief.py \
     --evidence evidence.json \
     --brief brief.md
   ```

   To avoid ordinary agent system-prompt contamination, use the direct local LLM
   synthesizer when a polished manager or executive brief should be written by a
   trusted local model:

   ```bash
   uv run scripts/github_work_rollup.py \
     --config .local/github-work-rollup.yaml \
     --layout executive \
     --format json \
     --output .local/github-work-rollup/evidence.json

   uv run scripts/synthesize_work_brief.py \
     --evidence .local/github-work-rollup/evidence.json \
     --audience executive \
     --report-recipient "Example leader" \
     --brief-output .local/github-work-rollup/brief.md \
     --warmup
   ```

   The synthesizer reads `references/prompt-contract.md` as the exact system
   prompt, sends the evidence JSON as the user prompt, uses the `local-llm`
   model role `work_brief_writer` by default, and runs `verify_work_brief.py`
   unless `--no-verify` is explicitly supplied for debugging.

   Emphasize:
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
