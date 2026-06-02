---
name: people
description: Resolve named humans, collaborators, users, reviewers, assignees, managers, clients, contacts, GitHub handles, nicknames, aliases, or likely misspellings into private local identity and contact context when identity may affect communication, routing, memory cleanup, rollout friction, GitHub planning, reviews, summaries, or follow-up. Use the optional `.local/people.yaml` identity index and resolver when available, and continue normally when no local people context is configured.
metadata:
  short-description: Resolve private local people context
resources:
  - path: scripts/resolve_person.py
    kind: script
    description: Resolves a person mention against the private `.local/people.yaml` index.
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
workflow_defaults:
  - name: people_index
    value: .local/people.yaml
    description: Optional private YAML identity and contact index.
  - name: details_file_prefix
    value: people/
    description: Optional details_file prefix resolved under `.local/`.
---

# People

Use this skill as a private local identity layer. It answers who a named person
or handle refers to; it does not decide workflow ownership by itself.

## Core Boundary

- `people` owns identity, aliases, bot aliases, handles, contact surfaces,
  company/team/title, actor trust/posture hints, relationship hints, and optional
  private profile notes.
- Workflow skills own their workflows. For example, `github-plan` decides which
  manager owns a planning item, while `people` can resolve that manager's local
  person id or GitHub handle.
- Repo metadata remains public-safe repo behavior: docs paths, quality gates,
  health checks, cleanup policy, and conceptual product ownership.

## Local Data

The private index is optional and gitignored:

```text
.local/people.yaml
.local/people/<person-id>.md
```

If `.local/people.yaml` is absent, continue normally without local identity
context. Do not treat missing people data as a failure.

Use `references/people.local.example.yaml` as the public-safe template. Real
names, handles, emails, phone numbers, company notes, and relationship details
belong only in ignored local files.

People entries may include lightweight `trust` hints for GitHub actors and other
collaborators. Treat trust as private operational posture: how much to verify,
how cautious to be with code or instructions, and whether the actor is known.
Never publish trust notes or use them to skip live verification.

## Resolution Workflow

When identity context may matter:

1. Resolve each named human reference with the helper from this skill directory,
   then branch on the returned status:

```sh
uv run people/scripts/resolve_person.py "<name-or-handle>"
```

- If `status` is `matched`, use only the resolved person's task-relevant fields.
- If `status` is `ambiguous`, ask a short clarification before relying on
  person-specific context.
- If `status` is `not_found` or `no_index`, proceed without enrichment.
- Load a linked detail file only after one person resolves and only when richer
  context is needed for the current task.

Prefer exact configured aliases and handles over fuzzy guessing. Never use fuzzy
or ambiguous resolution for write actions such as assigning, mentioning, routing,
or commenting. Treat any non-`exact`, non-`id`, non-`contact`, non-`name`, or
non-`compact` confidence as lookup-only context.

## Artifact Review Workflow

When `memory-distillation` or `rollout-friction` creates ignored local artifacts
such as `.local/rollout-memory/<run-id>/`, `.local/scan-output/<run-id>/`,
apply plans, reducer inputs, prompts, or reviewed batch results, use this skill
to review those artifacts for person facts before closeout if any artifact has
`people_updates`, `people_resolver_smoke_checks`, or visible person names,
handles, aliases, reviewer/assignee/manager fields, or contact/routing notes.

1. Load the small `.local/people.yaml` index when available, and build search
   terms from each known person's id, display name, preferred reference,
   aliases, handles, bot aliases, and compact forms.
2. Search the new local artifacts for every known form, not just the name that
   appeared in the final reducer plan. For example, searching only `Rob Burnett`
   can miss evidence that says `Burnett`, and searching only a handle can miss a
   full-name correction.
3. Also inspect any artifact-level smoke-check lists such as
   `people_resolver_smoke_checks`; unresolved natural names or handles should be
   treated as apply-review blockers until manually classified.
4. Discount matches that appear only inside encoded blobs, screenshots, binary
   payloads, tool command echoes, or the current review conversation. Those are
   search artifacts, not person evidence.
5. Promote only verified durable identity/contact/role/routing facts into
   `.local/people.yaml` or `.local/people/<person-id>.md`. Keep transient issue
   status, CI results, one-off reviews, and stale operational state out of the
   people index.
6. If an artifact mentions a person but evidence is incomplete, add only a
   minimal known-person entry after user approval, or leave a private TODO in
   the artifact review notes. Do not invent handles, roles, or relationships.

## Matching Model

The resolver normalizes input by trimming whitespace, stripping a leading `@`,
case-folding, normalizing Unicode, collapsing whitespace, and comparing compact
forms that ignore spaces, dots, hyphens, and underscores.

Matching order:

1. Exact normalized person id or `person:<id>` reference.
2. Exact normalized configured contact handle, including GitHub, Slack, Discord,
   email, and other contact keys.
3. Exact normalized display name, preferred reference, alias, or known
   misspelling.
4. Compact normalized exact match.
5. Optional conservative fuzzy match only when explicitly requested and exactly
   one candidate is obvious.

If more than one person matches at the winning tier, the resolver returns
`ambiguous` and omits detail files and notes.

If an issue, comment, PR, review, or commit actor does not resolve to a known
person or configured bot alias, treat the actor as unknown: verify claims from
live evidence, avoid assuming intent or authority, and call out the uncertainty
when it affects the work.

## Privacy Rules

- Never copy private mappings, contact details, notes, or profile files into
  public GitHub issues, PRs, comments, committed docs, examples, or logs unless
  the user explicitly asks for a sanitized public summary.
- Do not dump the whole people index. Surface only the fields relevant to the
  current task.
- Contact details are private but not secrets: they may live in ignored local
  people config when useful for routing, but must not be published or treated as
  credentials. Tokens, passwords, API keys, credentials, private messages, and
  sensitive personal data do not belong in the people index.
- Trust hints, actor posture, and bot ownership are private local context. Do not
  quote them into public GitHub artifacts; summarize only the operational effect
  when needed, such as “unknown actor; verified independently.”
- Verify live/current GitHub activity before making claims about recent work,
  comments, PRs, or reviews. Local notes are context, not live evidence.

## Optional Consumers

Other skills may reference this local data contract, but must remain portable:

- Use people context only when `.local/people.yaml` or the resolver is available.
- Continue normally when it is absent.
- Do not add hard dependencies on this skill until the skill system has an
  explicit dependency mechanism.

Useful soft consumers include:

- `github-plan`: resolve manager, assignee, reviewer, or handle values while
  keeping planning workflow authority in GitHub planning config.
- `memory-distillation`: move durable person identity/contact facts out of
  memory and into private local people config.
- `rollout-friction`: classify repeated wrong-person, stale-handle,
  wrong-manager, or unclear-contact failures as identity friction.
- GitHub work rollups: resolve report subjects and handles before collecting
  live activity.
