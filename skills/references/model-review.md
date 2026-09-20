# Reviews By Another Model

Use this when asking another model to review work, and when weighing what a
model reviewer returns, including a background review a host starts by itself.
Findings from either are weighed the same way. Two things are not covered: a
human reviewer, whose comments follow the owning skill's rules, and a
deterministic analyzer such as a type checker or security scanner, whose output
is evidence rather than an opinion to weigh.

## When To Ask For One

Ask when a mistake would reach widely and nothing executable would catch it:
shared instructions, approval and safety rules, destructive or irreversible
helpers, and contracts other repositories depend on. Routine code that tests
already cover does not need one; the tests are the review.

These points apply to a review you commission, not to one that already ran. The
`model-review` skill runs one: it starts each provider's model read-only and
fails loudly when a reviewer could not read or returned nothing.

- Include at least one reviewer from a different provider than the author. A
  model from the same provider shares the author's blind spots. When no other
  provider is reachable, say so and use what is available; do not present a
  same-provider run as independent.
- Name reviewers by provider and model, and record the model the run actually
  used. Independence comes from the model, and one harness can run several.
  Mention the harness only to explain how a run was invoked or a sandbox effect.
- Give the reviewer the change, the paths to read, and what the change is for.
  Withhold the author's argument that it is right, and the expected answer.
- Let the reviewer read the files with its own tools instead of pasting their
  content into the prompt. Pasted content hides the neighboring files where
  conflicts live, and fills the reviewer's context with text it cannot search.
  If a reviewer cannot read files, fix its access or give it absolute paths.
- Ask for a concrete, realistic way each problem would occur, and a severity.
  Let the reviewer answer "none". A prompt that demands a list gets one.

## Weighing Findings

A finding is a hypothesis, not a task.

- Act on it when it reproduces in a realistic scenario, or when the harm would
  be severe or irreversible even if it is unlikely. When a finding is about
  agent behavior, reproduce it by running the scenario, not by rereading the
  text. Behavior is probabilistic: one clean run does not refute a finding that
  says "sometimes", so say how many runs, and on which models.
- Decline it when the evidence refutes it, or when no realistic situation
  produces it, and record why where the change is reviewed. Declined with a
  reason is an accounted-for outcome, the same as fixed, deferred, or tracked,
  and does not block readiness or closeout.
- Could not test is not refuted. When you lack the platform, access, or harness
  to check a plausible finding, record it as unresolved with what is missing,
  and defer or track it like any other open item. Do not decline it.
- Do not add a test, a code branch, or a sentence for a case nobody can produce.
  That is how review turns into bloat: each item sounds reasonable alone, and
  together they bury the behavior that matters.
- Make the smallest change that removes the demonstrated problem. A reviewer's
  proposed wording or patch is a suggestion.
- When reviewers disagree, or you disagree with one, test it rather than
  averaging the opinions.

## Reporting

In the pull request or the owning issue, give the reviewers by provider and
model, what was acted on, what was declined and why, and what was reproduced.
Keep it short; the declined list is what keeps dissent visible without turning
it into code.
