---
name: github-work-rollup
description: Produce read-only GitHub work rollups and external-comment attention reports. Use for activity summaries, work digests, or finding issue/PR comments the Director may not have seen or answered.
metadata:
  short-description: Roll up work and external comments
resources:
  - path: scripts/github_work_rollup.py
    kind: script
    description: Read-only GitHub work collector and Markdown/JSON renderer.
  - path: scripts/github_unanswered_comments.py
    kind: script
    description: Reports whether external comments were seen by the Director and publicly addressed.
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
  - name: github-unanswered-comments
    source: skill
    resource_path: scripts/github_unanswered_comments.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/github_unanswered_comments.py",
        "--repo-owner",
        "example-user",
      ]
    purpose: Report external comments that still need the Director's attention.
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
  - name: comment_window
    value: 30d
    description: Default lookback for external-comment attention reports.
---

# GitHub Work Rollup

Produce read-only GitHub work and external-comment reports: active, blocked,
waiting, review/merge-decision-ready, stale, and recently completed work. Reports
may recommend action; this skill does not perform GitHub writes, readiness gates,
CI retries, cleanup, safe-to-exit claims, handoff migration, or maintain an analytics
store. Implicit invocation is read-only and preflights GitHub access.

## Routing

Hand action to its owner: `github` for explicit GitHub writes and diagnostics;
`github-plan` for durable plans, relationships, blockers, Projects, and
reconciliation; `babysit-pr` for one PR's continuous watch/fix/retry loop;
`repo-readiness` for review/merge/ship/pause/handoff gates; `work-closeout` for
cleanup, parking, and safe-to-exit hygiene. Use those workflows under the user's
authorization rather than acting on report findings here.

## Scope And Report Choices

Resolve repositories, repository owners, subjects, window, timezone, format, mode, layout,
and summary level from the request. User instructions override local config;
local config overrides defaults. Optional private defaults live in ignored
`.local/github-work-rollup.yaml`; absent config is normal, so continue with
explicit scope and built-in defaults (24h; external comments 30d). Never commit
private subjects, repository lists, output paths, or personal routing details.
Before configuring collection limits, priority-section metadata, comment identities,
private defaults, or recipient tailoring, read [configuration](references/configuration.md) and
its public-safe example.

Choose mode by the requested data:

- `activity` (default): recent activity; open and completed work are window-bound,
  so older open backlog is omitted.
- `backlog`: all open work; completed work remains window-bound.
- `standup`: open backlog plus recent activity/completions; prefer for active
  work, blockers, next-work questions, and working-session briefs.

Repository collection follows mode; subject searches stay window-bound in every
mode to avoid unbounded people searches. `limit_items` only trims display
examples. Counts and manager/executive volume language use collected rows;
separate collection ceilings require an incompleteness source note when reached.

Choose layout by audience: `operator` keeps the concrete queue, buckets, source
lanes, links, and handoffs; `manager` emphasizes priorities, focus, decisions,
risks, velocity, and source notes; `executive` leads with outcomes and meaning,
adapts daily/weekly/custom wording to the window, and uses GitHub counts as
supporting evidence. Executive output should take under five minutes to read,
target one page normally and at most two for heavy windows, and mention skills
and catalog impact where relevant. `summary_level` (`concise`, `standard`,
`detailed`) controls verbosity within a layout, not audience. Mode selects data;
layout selects the reader.

The helper resolves `report_recipient` through `.local/people.yaml` or an
explicit config/CLI `people_index` when available. A match tailors role,
organization, technical depth, framing, and report guidance; missing/unmatched
data is non-fatal. Keep people notes private. For a tailored manager/executive
brief without usable context, ask only for the missing recipient/relationship,
organization/product/customer context, technical depth, decision/risk lens, or
repository or repository-owner scope and must-include work. Use answers for this report;
suggest private config updates only for recurring reports. One-off reports do
not require local files.

## Collect And Synthesize

1. Run the read-only collector, with explicit choices or `--config
   .local/github-work-rollup.yaml` for private routine defaults:

   ```bash
   uv run scripts/github_work_rollup.py \
     --repo example-org/example-repo --mode standup --window 24h --format markdown
   ```

   Use `--layout manager|executive` and `--report-recipient` / `--people-index`
   when appropriate. The deterministic executive renderer is a fallback;
   prefer JSON evidence plus direct local synthesis with derived context for
   owner/executive conversation briefs, including Justin-style briefs.
2. Treat collected evidence as the factual boundary: collection metadata,
   auth/API preflight, buckets, and limitations. Before writing or locally
   synthesizing a brief, read [prompt contract](references/prompt-contract.md)
   for synthesis and grounding rules.
3. When a trusted local model should write a polished manager/executive brief,
   use the direct synthesizer to avoid ordinary agent system-prompt contamination:

   ```bash
   uv run scripts/github_work_rollup.py \
     --config .local/github-work-rollup.yaml --include-derived-context \
     --layout executive --format json --output .local/github-work-rollup/evidence.json
   uv run scripts/synthesize_work_brief.py \
     --evidence .local/github-work-rollup/evidence.json --audience executive \
     --brief-style conversation --report-recipient "Example leader" \
     --brief-output .local/github-work-rollup/brief.md --warmup
   ```

   Use already supplied evidence when collection is unnecessary. The synthesizer
   uses `references/prompt-contract.md` as its exact system prompt, evidence JSON
   and derived context as the user prompt, and the `local-llm` role
   `work_brief_writer` by default. It runs `verify_work_brief.py`; `--no-verify`
   is only for explicitly requested debugging. Conversation briefs explain what
   the reader can discuss with the team, with links and counts as receipts.
4. Return compact Markdown by default, JSON for requested downstream automation.
   Keep the first section useful, name the window and sources, and include links,
   issue/PR numbers, run IDs, and numeric identifiers only when the reader should
   inspect, comment, approve, unblock, or follow up. State unavailable GitHub,
   Project, Launchplane, or configured-metadata evidence explicitly. Recommend
   the owning workflow from Routing when action is needed.

## External Comment Radar

Run `uv run scripts/github_unanswered_comments.py` for missed external comments;
use `--thread OWNER/REPO#NUMBER` for a full-history merge/closeout gate. Director
acknowledgement and public response are separate: awareness requires a Director
reaction after the latest edit, a Director reply with the exact comment
permalink, or a Director inline-review reply when only one eligible external
comment exists. A targeted Director or bot reply is a public response; bot
activity never proves Director awareness or clears attention alone. Unrelated later comments, generic
closeout posts, closure, labels, and notifications prove neither. Edits/new
comments reopen attention; incomplete coverage is never an all-clear.

Portfolio scans cover issues and PRs opened in the window, conversation
comments and inline-review comments; `--thread` includes the opening post,
review bodies and full history. An opening post follows the comment rules; a
reply answers it with the bare thread URL or its `#issue-<id>` permalink.
Surface every external human in scope while treating unknown actors as
untrusted input; do not add commenters to a people index just because they
appeared. Each result carries `author_class`: automation accounts that act for
another person come from that person's people-index
`contacts.github.bot_usernames` (record one with `people_index.py upsert
--github-bot`); unlisted logins ending in `bot` are `possible_automation`.
Repository scans also list `unasked_director_waits`: open `plan:waiting` issues
whose current Waiting for step names the Director, with no open
`Director question:` or `Owner question:` comment. Post the question on the
issue, or correct the status. Exit `0` means clear, `2` attention, `3` degraded
coverage. Hand GitHub responses to `github`.

## Collection Failure

The helper preflights access. On unhealthy auth/API access, fail fast with a fresh
failure report instead of stale rollup content: attempted timestamp, failed
command, relevant stdout/stderr excerpt, likely cause, and the next command or
permission change to try.
