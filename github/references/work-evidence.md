# GitHub Work Evidence

`github/scripts/github-work-evidence.py` collects bounded, read-only GitHub work
evidence as JSON. It is an evidence source, not a report writer.

Use it when another skill needs broad GitHub facts across repositories, owners,
subjects, releases, workflow runs, and mechanical work buckets. Downstream
skills may interpret the evidence, but this helper must not perform writes or
produce audience-specific prose.

## Contract

The helper emits JSON with:

- `scope`: repositories, subjects, mode, and collection lanes.
- `summary`: deterministic counts computed from collected facts.
- `buckets`: mechanically classified work items with source URLs and states.
- `priority_sections`: configured priority evidence, when present.
- `releases` and `workflows`: window-filtered release and workflow evidence.
- `source_notes`: collection warnings, truncation notes, and scope limitations.

Counts, buckets, and source notes are computed in code so downstream LLM prompts
do not need to perform arithmetic or infer collection limits.

## Boundaries

- Keep this helper JSON-only.
- Do not add `manager`, `executive`, or other audience layouts here.
- Do not use private people profiles, recipient tailoring, or prose framing.
- Do not mutate issues, PRs, Projects, checks, workflow runs, or labels.
- Do not treat raw activity volume as plan direction; `github-plan` owns durable
  planning meaning and issue-graph state.

## Consumers

- `github-plan` may use the evidence when reconciling plan issues, blockers, or
  Project views.
- `repo-readiness` may use the evidence alongside local gates and CI to decide
  readiness.
- `work-closeout` may use the evidence to park or close a workstream cleanly.
- `work-brief` uses the evidence as source material for LLM-led briefs, with
  citations, source limitations, and explicit uncertainty.

## Config

Private routine defaults live in `.local/github-work-evidence.yaml`. The config
may combine owner/user scans with explicit repositories:

- `repo_owners`: GitHub users or organizations whose non-archived repositories
  should be scanned, bounded by `limit_repos`.
- `repositories`: individual `OWNER/REPO` entries to always include or to add
  alongside owner scans.
- `subjects`: GitHub logins to highlight in recent activity searches.
- `priority_sections`: named high-signal repo groups that downstream briefs
  should consider first when deciding what matters.

Supported evidence config keys are `timezone`, `default_window`, `subjects`,
`repo_owners`, `repositories`, `mode`, `collection_limit_items`,
`release_collection_limit`, `workflow_collection_limit`,
`include_external_activity`, `include_bots`, `noise_filters`, and
`priority_sections`. Use explicit CLI flags for one-off runs.

Audience, recipient, and report-writing configuration belongs in downstream
briefing skills, not in this evidence helper.

The collector lives at `github/scripts/github_work_evidence_collector.py` and is
loaded by `github-work-evidence.py` as a JSON-only evidence module. Keep
downstream consumers pointed at this contract rather than presentation-specific
reporting code.
