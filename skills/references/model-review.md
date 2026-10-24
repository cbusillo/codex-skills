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
  model from the same provider shares the author's blind spots. Follow the
  standing provider decisions through `model-review`. When no allowed provider
  is reachable, report the outcome and keep that decision in force; do not
  substitute a provider through another command.
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
- Include this question in each review: "Does this PR add a test that
  pins a literal, version, wording or config text?" Identify a realistic
  intended change that would break it, and distinguish behavior checks,
  agreement with one source of truth, and real artifact or immutable-evidence
  checks. The warning scanner and its CI wiring are documented in
  [test quality](test-quality.md). This question does not expand when a review
  is required; the criteria above still own that decision.
- Also ask: "Read the Director's overall DIRECTION.md and the repository's
  own, if it has one. Does this work fit them, and would they prefer a simpler
  way?" Reviewers have no network. Pass the overall file, from a checkout
  current with its remote, with the helper's `--input`. A finding that the work should stop
  or change course is weighed under the escalation rule below.
- When commits carry
  [Client-impact trailers](../github/references/repo-workflow.md#client-impact-trailers),
  ask the reviewer to check each `none` claim against that commit's diff. A
  commit that could change what the Client saw or accepted is a blocking
  finding; fix it with a `Client-impact: visible` commit so the Client decides
  again.
- When a PR has Client test notes (or legacy Owner test notes), ask the reviewer to check its
  `Client change:` or `Nothing for the Client to test:` statement against the
  diff ([ready handoffs](../github/references/cli-reference.md#ready-handoffs)).
  A wrong class in either direction is a blocking finding.

## Before A Decision

A review can cover a pending decision instead of a change. When the Director
asks whether a choice is aligned, ask for this review even when the criteria
above would not. Put the question on its issue first. Save the issue's body
and comments, without your recommendation, to a file outside the checkout and
pass it with `--input`. Run it from a clean checkout of the default branch,
so the helper names no diff. Post the weighed result on the issue; the
Director decides. The reviewer is not the direction agent, so this needs no
direction session.

## Weighing Findings

A finding is a hypothesis, not a task. It names its trigger and evidence, and
the author verifies it.

- Act on it when it reproduces in a realistic scenario, or when the harm would
  be severe or irreversible even if it is unlikely. When a finding is about
  agent behavior, reproduce it by running the scenario, not by rereading the
  text. Behavior is probabilistic: one clean run does not refute a finding that
  says "sometimes", so say how many runs, and on which models.
- Fix every reproduced defect in this PR, at any severity. Prove the fix with
  the cheapest check that would catch it coming back: a static check (lint,
  type or inspection rule) first, then an existing test extended, then a new
  test only when neither fits. That is one new test per kind of bug, never one
  per instance.
- On the first real defect, fix the whole kind (one parser, one check), and ask
  the reviewer for sibling paths. After two fix-and-review cycles, stop
  patching: brief a fresh agent, get an outside design look, then redesign.
- A real bug outside the PR's scope gets its own small fix PR right away, not a
  parked issue.
- Decline a non-defect, or a finding no realistic situation produces or the
  evidence refutes, with one line where the change is reviewed and no issue.
  Fixed or declined with a reason is accounted for and does not block
  readiness or closeout. Deferred or tracked accounts only for an unresolved
  finding (below), never for a reproduced defect.
- Could not test is not refuted. When you lack the platform, access, or harness
  to check a plausible finding, record it as unresolved with what is missing,
  and defer or track it like any other open item. Do not decline it.
- Do not add a test, a code branch, or a sentence for a case nobody can produce.
  That is how review turns into bloat: each item sounds reasonable alone, and
  together they bury the behavior that matters.
- Under the global Tests rules, never add a test that asserts a literal defined
  elsewhere, workflow or config text, or a change-detector. Deleting a test that
  fails the keep-only-if rule is an improvement, not lost coverage.
- Make the smallest change that removes the demonstrated problem, including its
  whole kind. A reviewer's proposed wording or patch is a suggestion.
- A rereview covers the changed behavior plus what it touches. A purely local,
  additive fix can skip a full rereview.
- When reviewers disagree, or you disagree with one, test it rather than
  averaging the opinions.
- The rules above cover findings that add something. A finding that would
  delete, retire, stop, or redirect work is the Director's to decide: escalate it
  under the `direction` skill instead of acting on it or declining it. An
  author judging a challenge to its own plan is how a wrong direction survives
  review.

## Reporting

In the pull request or the owning issue, give the reviewers by provider and
model, what was acted on, what was declined and why, and what was reproduced.
For a review that gates ready or auto-enqueue, post the weighed disposition
through the existing PR comment helper:

```sh
uv run <github-skill-dir>/scripts/gh-pr.py --repo OWNER/REPO comment N --body-file disposition.md --reviewed-head FULL_REVIEWED_SHA
```

The helper adds explicit head-bound `Review result: weighed` metadata. This
attests that the completed review was weighed; use ordinary comments for
requests, launches, partial reports and the Supervisor's raw findings. The
body stays free text: Acted on, Declined, Deferred, Tracked and Unresolved are
all valid dispositions. No vocabulary in that body can complete a review.
A new head needs a new weighed record; rerun the model only when the review
policy or task requires it. Existing completed reviews can be weighed and
recorded through the helper without repeating inference just to add metadata.
See [ready handoffs](../github/references/cli-reference.md#ready-handoffs).
Keep it short; the declined list keeps dissent visible.
