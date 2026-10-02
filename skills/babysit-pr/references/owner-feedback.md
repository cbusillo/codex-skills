# Launchplane Client Feedback

Read this before acting on a snapshot that contains `owner_review_items`,
`owner_review_errors`, `address_owner_review_changes`,
`review_owner_feedback_history`, `owner_feedback_delivery_pending`, or
`owner_review_verification_unavailable`.

## What the watcher verifies

`owner_review_items` retains the complete decision prose on every snapshot,
even after its comment ID was seen. A marker from any publisher is only a
candidate: the watcher verifies every decision and its exact comment receipt
through the private scoped Launchplane read, and never sends credentials to a
comment-supplied URL. Bot comments without the Client marker follow normal
filtering. Failed verification is explicit, not evidence that no feedback
exists.

Once a Client channel is known, the watcher also reads its latest saved
decision, so a newer request with pending publication cannot hide behind an
older acceptance.

## Actions

- `owner_feedback_delivery_pending` blocks readiness and retains the latest
  prose.
- `owner_review_errors` and `owner_review_verification_unavailable` block merge
  readiness while CI and PR monitoring continue. Previously verified prose is
  retained with `verification_status: unavailable` until the read recovers; do
  not treat it as a newly verified decision. Diagnose the scoped read without
  changing credentials or grants, and hand off a persistent denial or damaged
  projection.
- `address_owner_review_changes` means the latest decision for the current head
  requests changes. Older revisions remain visible as history; acceptance there
  does not approve the current head.
- `review_owner_feedback_history` calls out the latest historical request. A new
  commit alone does not prove it was addressed: explain how the current work
  addresses it, or explicitly hand off what remains, before reporting
  readiness.

## Handling

Read and summarize the Client's reason before changing the product. Treat it as
human product feedback, including the usual limits on replying to a human.
Client projections are issue comments with no review thread to resolve: preserve
the comment and report how the work addressed it; do not reply automatically.

A projected acceptance never grants merge or deployment authority. The existing
Client-review status on marked PRs continues to govern the need for a fresh
Client decision. If Launchplane's status or review page says delivery is
pending, use the `launchplane` skill's Client-review reader to inspect the saved
decision; a short status alone is insufficient.
