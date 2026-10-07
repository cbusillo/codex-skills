# Work rollup configuration

Read before configuring collection limits, priority-section metadata, private
defaults, comment identities, or recipient tailoring. User instructions override local config; local
config overrides defaults. Use
[`github-work-rollup.local.example.yaml`](github-work-rollup.local.example.yaml)
for the public-safe shape. Keep private values in ignored local files.

The collector verifies authentication with `gh-with-env-token --check`, which
supports App installation tokens and configured user tokens and reports the
actor in the evidence JSON. It then checks read access to the first configured
repository and repository owner, or issue search for a subject-only scope.
`GITHUB_WORK_ROLLUP_GH` overrides must support the wrapper's `--check` command
and its actor output. Failed authentication or scope checks stop collection.

Supported config fields:

- `timezone`
- `default_window`
- `report_recipient`
- `people_index`: optional private people index path for recipient tailoring and for the comment radar's Director names and automation accounts; the radar otherwise reads the people skill's default indexes
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
- `include_derived_context`: collect bounded repository metadata and README
  excerpts as provenance-backed context for local LLM synthesis
- `context_repo_limit`: maximum repositories to enrich with derived context
- `include_external_activity`
- `include_bots`
- `noise_filters`
- `priority_sections`
- `comment_window`: external-comment lookback; defaults to `30d`
- `comment_self_logins`: the Director's accounts whose reactions and targeted replies count
- `comment_bot_logins`: automation accounts whose targeted replies count

Each `priority_sections` entry may also include executive-facing metadata:

- `portfolio_area`: broad bucket or product area, such as an internal planning
  section name
- `workstream`: canonical workstream name to render in executive briefs
- `relationship`: plain-language relationship between the workstream and the
  portfolio area
- `initiatives`: compact list of named initiatives inside the workstream

Use these fields when a GitHub grouping label is broader than the work it
contains. For example, a portfolio area can be "Example Product Area" while
the workstream remains "Example Workstream" and the initiative is "Example
Initiative". If these
fields are absent, executive rendering infers a workstream from item titles, but
explicit metadata is more reliable.
