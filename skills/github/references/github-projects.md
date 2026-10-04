# GitHub Projects as Automatic Views

Read when using a configured or requested Project, or another local planning
view. GitHub issues, milestones, and native relationships remain the source of
truth. Keep planning status and Finish Line in the issue graph; Projects display
that record.

## Automatic Fields

Keep Status on close, Parent, Sub-issue progress, Milestone, and Repository.
Configured `create` still adds the issue to the Project, and `close` still sets
its Status to Done. Native hierarchy and issue metadata supply the other fields.

Do not maintain Focus, Manager, Finish Line, Roadmap Start, or Roadmap Target in
Projects during routine planning. Existing fields and items remain in place;
there is no field deletion or backfill. Project #4 (Code Plans) is an automatic
view of the direction Track issues and their product-repository sub-issues.

## Explicit Field Edits

`project-set` remains available for a Director's explicit field-edit request;
with no values it writes nothing. `create --focus` and `create --manager` also
remain explicit edits, but configured Manager defaults are not copied.
`create --finish-line` updates only the issue body. Use `project-set --finish-line`
only when the Director explicitly requests that Project field edit.

Explicit `person:<id>` manager values use the optional `people` skill and local
people context, preferring `preferred_reference` and then `display_name`.
Unresolved references are skipped. Raw manager strings and handles are unchanged.

## View Synchronization

When an issue operation succeeds with a non-blocking Project warning, report
that split outcome and the helper's choices. Do not repeatedly retry or silently
switch to human authentication. The Director chooses whether to grant automation
Project access, use Project-capable auth, disable synchronization, or correct
stale configuration. Do not treat an unavailable view as missing issue data.

## Local Context Views

If Launchplane or another configured context helper is useful, call it once
before or alongside `index`. Unavailable, unauthorized, invalid, or missing
context is normal absence; continue with GitHub-only planning. Treat view
output as a source-link or inspection hint, not runtime authority or a second
plan. Do not copy private payloads into public issues, PRs, or handoffs without
reviewing them for public safety.
