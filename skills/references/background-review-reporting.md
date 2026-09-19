# Background Review Reporting

Use this contract when readiness, closeout, or GitHub work reports Every Code
auto-review, Automatic Background Review, or an equivalent review lifecycle
that can start after the assistant's final response.

## Point-In-Time States

Match every observation to the active repository, branch or PR, and head SHA.

- `not yet observable`: the lifecycle surface is readable, but no matching
  lifecycle evidence is visible at observation time. This includes both the
  period before a possible post-turn trigger and a later observation where no
  review ever started. It is non-terminal and does not imply clean, skipped,
  disabled, or failed.
- `in flight`: a matching review started and has no terminal outcome yet.
- `observation unavailable`: the lifecycle surface could not be read. This is
  an evidence gap, not proof that no review exists.
- `completed`, `cancelled`, `superseded`, `failed`, and `skipped`: terminal
  states only when positive matching lifecycle evidence reports that outcome.

When the lifecycle surface is readable, absence of matching evidence is always
`not yet observable`; never infer `skipped`, `not emitted`, `did not run`, `no
review`, or another terminal claim from it. If the surface cannot be read,
report `observation unavailable` instead of treating the failed observation as
absence.

## Decision Rules

- Take a point-in-time read when the surface is available. Do not poll, sleep,
  or delay a final response solely to wait for a review that can only start
  after that response.
- `not yet observable` does not block `Safe to exit: yes` when every other gate
  is satisfied. State the observation time or target SHA and note that a
  post-turn review may still start.
- A matching `in flight` review remains pending evidence and keeps readiness or
  safe-to-exit conditional under the owning skill's existing gate policy.
- `observation unavailable` must stay visible as an evidence gap. Do not report
  the review as clean or terminal. Keep readiness or safe-to-exit conditional
  unless the owning policy explicitly says that review surface is not required
  for the current task.
- Preserve an observed terminal state exactly. A cancellation or supersession
  is terminal but is not a clean review, and a completed review still requires
  its findings or no-findings result to be accounted for.

## Durable Wording

Write GitHub comments and handoffs as point-in-time evidence. A suitable absence
statement is: `Background Review: not yet observable for <target> as of <time>;
a post-turn review may still start.`

The never-started exception applies only when a later observation can read the
lifecycle surface and still finds no matching evidence because no review ever
started. In that case the earlier statement needs no corrective follow-up.

If a later session instead observes matching terminal evidence, always add a
follow-up with that exact state and its run, comment, or ledger identifier; the
never-started exception cannot apply. Preserve the earlier, accurate
point-in-time statement rather than rewriting it.
