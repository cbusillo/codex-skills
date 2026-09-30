---
name: people
description: Resolve named humans, collaborators, users, reviewers, assignees, managers, clients, contacts, GitHub handles, nicknames, aliases, or likely misspellings into private local identity and contact context when identity may affect communication, routing, memory cleanup, rollout friction, GitHub planning, reviews, summaries, or follow-up. Use the optional global people index under Code home plus repo-local `.local/people.yaml` overlays when available, and continue normally when no local people context is configured.
metadata:
  short-description: Resolve private local people context
resources:
  - path: scripts/resolve_person.py
    kind: script
    description: Resolves a person mention against global and repo-local private people indexes.
  - path: scripts/people_index.py
    kind: script
    description: Writes scoped private people index entries, defaulting to global/user storage.
  - path: scripts/test_resolve_person.py
    kind: script
    description: Regression tests for the people resolver.
  - path: references/people.local.example.yaml
    kind: reference
    description: Public-safe example schema for the private people index.
  - path: references/migration.md
    kind: reference
    description: Migration guide for consolidating private person facts into the people index.
commands:
  - name: resolve-person
    source: skill
    resource_path: scripts/resolve_person.py
    example_argv: ["uv", "run", "people/scripts/resolve_person.py", "Example"]
    purpose: Resolve a named person, alias, or handle against local private identity context.
  - name: upsert-person
    source: skill
    resource_path: scripts/people_index.py
    example_argv:
      [
        "uv",
        "run",
        "people/scripts/people_index.py",
        "upsert",
        "--id",
        "example-person",
        "--display-name",
        "Example Person",
      ]
    purpose: Create or update a scoped private person entry, defaulting to global/user storage.
workflow_defaults:
  - name: people_index
    value: $CODE_HOME/skills/.local/people.yaml
    description: Optional global/user private YAML identity and contact index.
  - name: repo_people_index
    value: .local/people.yaml
    description: Optional repo-local private identity overlay for project-specific people or overrides.
  - name: details_file_prefix
    value: people/
    description: Optional details_file prefix resolved under the matched index scope's private `.local/` root.
---

# People

Resolve identity, aliases/bot aliases, handles, contact surfaces,
company/team/title, private profile notes, and trust/relationship hints. This
private layer does not decide workflow ownership: `github-plan` owns planning
routing, for example. Repo metadata keeps public-safe behavior such as docs,
gates, health, cleanup and conceptual product ownership.

## Optional Private Storage

Durable identity context defaults to global/user storage:
`$CODE_HOME/skills/.local/people.yaml`, with details in
`$CODE_HOME/skills/.local/people/<person-id>.md`. Helpers fall back from
`CODE_HOME` to `CODEX_HOME`, then `~/.code`; if none exists (for example on a
catalog-only host), they use `.local/` inside the catalog shipping the helper.

Repo-local `.local/people.yaml` and `.local/people/<person-id>.md` are overlays
for project-specific contacts, client context, overrides or supplements. The
resolver loads global entries first, then repo entries: the same `id` replaces
the global entry for that repo; new ids supplement it. Missing indexes are
normal: continue without enrichment, not an error.

Use [the public-safe schema](references/people.local.example.yaml) for the
optional gitignored index. Real names, handles, emails, phone numbers, company
and relationship facts belong only in ignored local files. Update through
`people/scripts/people_index.py upsert --id <id> --display-name "<name>"`, which
defaults to global/user storage; use `--scope repo` only for repo-specific
people/overrides/supplements. Read [migration](references/migration.md) before
consolidating identity facts from other local sources.

## Resolve Before Relying On Identity

1. Resolve each named human reference when context may matter, using the helper
   from this skill directory:

   ```sh
   uv run people/scripts/resolve_person.py "<name-or-handle>"
   ```

2. Branch on `status`: `matched` permits task-relevant fields; `ambiguous`
   requires a short clarification before relying on person-specific context;
   `not_found` or `no_index` means proceed without enrichment. Load a linked
   detail file only after one person resolves and richer context is needed.
3. For assigning, mentioning, routing, commenting or other writes, only
   `matched` with confidence `id`, `contact`, `name` or `compact` is write-safe
   identity context. Fuzzy, ambiguous and unknown-confidence results are
   lookup-only. Prefer configured aliases/handles over guessing.
4. Treat actors not resolved to a known person or configured bot alias as
   unknown: verify claims from live evidence, do not assume intent/authority,
   and state uncertainty when it affects the work. Local trust hints guide
   caution and verification; never use them to skip live checks.

The resolver trims whitespace, strips leading `@`, case-folds, normalizes
Unicode/collapses whitespace, and compares compact forms ignoring spaces,
dots, hyphens and underscores. Tiers in order: exact normalized id or
`person:<id>`; contact handles (GitHub/Slack/Discord/email/other keys); display
name/preferred reference/alias/known misspelling; compact exact match; optional
conservative fuzzy matching only when explicitly requested and one candidate
is obvious. A tie at the winning tier returns `ambiguous`, omitting notes and
detail files.

Refresh verified durable renames, handles, roles/relationships and repeatedly
unresolved natural names. Keep useful former spellings as aliases; do not call
an old alias stale without user or maintained-source confirmation.

## Conditional Artifact Review

Before closing out new ignored memory-distillation/rollout-friction artifacts
with `people_updates`, `people_resolver_smoke_checks`, or visible person names,
handles, aliases, reviewer/assignee/manager fields or contact/routing notes,
read and apply [artifact review](references/artifact-review.md). It owns the
search, evidence classification, unresolved-name blockers and promotion steps.

## Privacy And Portability

- Never publish private mappings, contacts, notes, profile files, trust/posture
  or bot ownership in public GitHub artifacts, tracked docs/examples or logs
  unless the user explicitly requests a sanitized public summary. Do not dump
  the index; surface only task-relevant fields. When necessary, report only an
  operational effect such as “unknown actor; verified independently.”
- Contact details are private, not credentials; ignored people config may hold
  them for routing. Tokens, passwords, API keys, credentials, private messages
  and sensitive personal data do not belong there.
- Verify current GitHub activity before claims about recent work, comments,
  PRs or reviews. Notes are context, not live evidence.
- Consumers use context only when an index or resolver is available and continue
  normally otherwise; do not add hard skill dependencies before an explicit
  dependency mechanism exists. Soft consumers include github-plan for people
  values, memory-distillation for durable identity migration, rollout-friction
  for identity mistakes, and work rollups for subjects before live collection.
