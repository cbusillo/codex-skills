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
- Future reporting skills may use the evidence as source material for LLM-led
  briefs, with citations and explicit uncertainty.

## Compatibility

The helper currently accepts the existing ignored local config shape used by the
legacy work-rollup helper. Audience/report-rendering keys such as `layout`,
`summary_level`, `report_recipient`, `people_index`, and `output_path` are
ignored for evidence output and surfaced in `source_notes` when present.

This is a transitional wrapper around the legacy work-rollup collector so the
GitHub skill family can expose the evidence contract before the reporting layer
is rebuilt. Keep downstream consumers pointed at the JSON contract here, not at
the legacy renderer internals.
