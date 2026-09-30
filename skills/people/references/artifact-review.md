# People Artifact Review

When `memory-distillation` or `rollout-friction` creates ignored local artifacts
such as `.local/rollout-memory/<run-id>/`, `.local/scan-output/<run-id>/`,
apply plans, reducer inputs, prompts, or reviewed batch results, use this skill
to review those artifacts for person facts before closeout if any artifact has
`people_updates`, `people_resolver_smoke_checks`, or visible person names,
handles, aliases, reviewer/assignee/manager fields, or contact/routing notes.

1. Load the small global/user and repo-local people indexes when available, and
   build search terms from each known person's id, display name, preferred
   reference, aliases, handles, bot aliases, and compact forms.
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
5. Promote only verified durable identity/contact/role/routing facts with
   `people/scripts/people_index.py upsert`, which defaults to global/user
   storage. Keep transient issue status, CI results, one-off reviews, and stale
   operational state out of the people index.
6. If an artifact mentions a person but evidence is incomplete, add only a
   minimal known-person entry after user approval, or leave a private TODO in
   the artifact review notes. Do not invent handles, roles, or relationships.
