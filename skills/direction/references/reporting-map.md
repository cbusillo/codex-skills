# Weekly reporting maps

Read this when drafting an optional map in a Director's overall `DIRECTION.md`
or interpreting `capacity.reporting` from the weekly check. The Director approves
the map through the normal direction-only PR. An executing session does not
edit direction to install it.

The map names reporting kinds using existing PR or linked-issue labels and
the audit's milestone Track and own-project evidence. It changes reporting
only. It supplies no claim admission, selection priority, labels, PR metadata,
limits, alarms or grants. The Order and Capacity prose stays as the Director
wrote it; numerical limits are never parsed.

## Format

Add one `## Reporting map` section containing exactly one JSON code block and
no other text. Its value is a nonempty ordered array, for example:

````markdown
## Reporting map

```json
[
  {"name": "direction", "evidence": "direction-only"},
  {"name": "live fix", "labels": ["live-fix"]},
  {"name": "milestone", "evidence": "milestone"},
  {"name": "sales-channel", "labels": ["sales-channel"]},
  {"name": "own projects", "evidence": "own-project"},
  {"name": "tooling", "labels": ["tooling"]}
]
```
````

This is an example, not OPW's sourced policy or an instruction to create those
labels. Choose existing labels that prove the kind: an ordinary `bug` label
alone does not establish a live fix. No catch-all tooling rule exists; label
tooling explicitly when that distinction matters.

Every rule has a unique nonempty `name` and either a nonempty `labels` array
or one `evidence` value. Labels match case-insensitively, with any listed label
on the PR or any issue it links through the existing closing-keyword, `Refs`
or native-link reader. Evidence values are `direction-only`, `milestone` and
`own-project`, each used at most once. `direction-only` is required first.
Milestone and own-project evidence use the existing graph and declaration;
the map does not replace the own-projects Order item, including explicit none.
Heading capitalization is ignored. A label may belong to only one kind.
Unknown keys, duplicate names/JSON keys, labels shared between kinds and malformed maps are reported as
invalid; they never silently select the legacy tooling fallback.

## One count for each PR

The map read from merged overall direction at the start of the check applies
to the entire window. A PR that changes that map does not classify itself under
its proposed rules or switch the rules midway through the week. This reports
the window under current direction, not policy as it stood at each merge.
Labels and Track links are also read as they stand during the check; changing
them can change a historical recount.

1. Read the complete changed-file list. A PR whose only changed path is root
   `DIRECTION.md`, in any repository, counts as the first rule's kind regardless
   of labels or Track links. A rename counts both its old and new path. A PR
   mixing a direction change with another path is unresolved, with reason
   `mixed_direction_change`; it never quietly counts as a live fix or tooling.
2. Otherwise take the first matching rule. For the example, a live-fix label
   beats a Track link, and a Track link beats sales-channel or tooling labels.
   Thus sales-channel counts only outside milestones unless the Director
   deliberately orders its rule earlier.
3. When the first matching rule is label-based, multiple matching label kinds
   are a conflict, even across linked issues. List that PR with
   `conflicting_labels` and the matching names for a person to resolve; count
   it in no named kind. An earlier matching evidence rule settles the kind
   without consulting lower label rules.
4. With no match, list the PR as `unmatched`. Unavailable files, links or labels
   remain unresolved with the missing evidence named. Never guess a kind from
   titles, admission kinds, or caps in prose.

Each distinct merged PR in the window contributes once: to one named kind or
to `unresolved`, which includes conflicts. Shares divide named-kind counts by
**all** merged PRs, including unresolved ones. Do not present a partial split
as settled or renormalize it over classified PRs.

## Output and validation

The opt-in result is additive at `capacity.reporting`. Existing `merged_by_rank`,
`own_share`, per-repository counts and quality signals retain their meaning.
Without a map this field and the map's extra file/label reads are absent.
`capacity.reporting` contains the rules and source, Capacity prose verbatim
(apart from surrounding section whitespace), `merged_total`, `merged_by_kind`,
`share_by_kind`, and PR links in `unresolved`. `basis` is `merged_pull_requests`.
Statuses are `complete`, `no_merges`, `needs_review` (unmatched/conflicting/mixed
PRs), `unknown` (incomplete evidence) and `invalid` (bad map).

Incomplete map, graph, repository, file, label or link reads keep the weekly
window open through `coverage_incomplete`; partial counts are visible. Invalid
maps keep that window open until a corrected direction PR merges, so the next
check can recount the entire window. Optional reporting failures change the
new report's status, while the legacy own-share status still depends only on
its existing evidence. The shared audit window deliberately waits for both
reports; while it is open the dates and counts can span more than a week.
Classification questions with complete reads are reporting work for a person,
not capacity alarms or admission refusals. The weekly check still stamps its
normal local audit marker, so use fixtures when testing implementation and
do not run a live check solely to test this feature.

Before calling a Director's mapping validated, obtain the exact merged Order
and Capacity text and a dated week's PR-by-PR hand classifications. Reconcile
every PR, explain precedence for overlaps and direction changes, and compare
the counts and shares including unresolved work. Fixture results prove the
general mechanism; they do not prove a Director's real weekly result.
