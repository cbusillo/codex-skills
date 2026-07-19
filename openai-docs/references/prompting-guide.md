# Prompting guidance for GPT-5.6

Use this offline guide when adapting prompts, tool descriptions, agent
instructions, or prompt stacks to GPT-5.6. Pair it with current OpenAI docs when
they are available.

GPT-5.6 works best when prompts define the outcome, important constraints,
available evidence, and completion bar, then leave room for the model to choose
an efficient path. Treat every reduction as an eval-backed change rather than a
reason to remove safety or product requirements.

## Simplify prompts first

Start with a prompt and tool set that already works. Remove one group at a time,
then rerun the same representative cases.

Trim:

- repeated statements of the same rule;
- style or process instructions that do not change behavior;
- examples that do not change behavior;
- scaffolding for behavior the model already performs reliably;
- tools and tool descriptions unrelated to the task;
- stale model-specific instructions.

Keep:

- the user-visible outcome;
- success criteria and stopping conditions;
- safety, business, evidence, privacy, and permission constraints;
- tool-routing rules whose choice depends on context;
- required output shape and validation requirements.

Review the remaining instructions for contradictions and specify precedence
when two constraints can conflict. Conflicting prompt contracts can create more
instability than missing detail.

## Outcome-first prompts

Describe the destination rather than prescribing every step. Use absolute terms
such as `always`, `never`, `must`, and `only` for true invariants. For judgment
calls, provide decision rules.

```text
Goal: Resolve the request end to end.

Success means:
- make the decision from available evidence
- complete every allowed in-scope action
- return the result, completed actions, and blockers
- ask only for the smallest missing fact when progress is unsafe without it
```

Add stopping conditions. Minimize unnecessary loops, but do not let loop
minimization outrank correctness, required evidence, calculations, citations,
or validation.

Preserve explicit user values. When a value is implicit, provide decision
criteria and let the model use context rather than a universal default or broad
keyword map.

## Personality, collaboration, and response length

Keep personality and collaboration instructions short:

- personality controls tone, warmth, directness, formality, humor, and polish;
- collaboration style controls when the model asks, assumes, takes initiative,
  explains tradeoffs, checks work, and handles uncertainty.

Neither replaces goals, success criteria, tool rules, or stop conditions.
GPT-5.6 is concise by default, so re-evaluate broad brevity instructions and
keep them only when they reliably improve the product output. Use
`text.verbosity` values `low`, `medium`, or `high` for a stable API default and
the prompt for task-specific length or format.

For editing and rewriting, say what must be preserved: requested artifact,
length, structure, genre, factual claims, and tone. Do not add claims, sections,
or promotional language unless requested.

## Define autonomy and approval boundaries

State what level of action the request authorizes so GPT-5.6 can continue safe,
in-scope work without pausing unnecessarily while stopping before destructive,
external, costly, or scope-expanding actions.

```text
For requests to answer, explain, review, diagnose, or plan, inspect relevant
materials and report the result. Do not implement changes unless requested.

For requests to change, build, or fix, make in-scope local changes and run
relevant non-destructive validation without asking first.

Require confirmation for external writes, destructive actions, purchases, or a
material expansion of scope.
```

Keep the policy in one place. Repeating `ask first` or `do not mutate` can block
safe work. For long-running work, name the current layer—research, design,
implementation, review, or external coordination—so the model does not silently
move into another layer.

## Tool routing

Expose only task-relevant tools. Tool descriptions should state what the tool
does, when to use it, important return fields, and error behavior.

- Resolve required discovery and validation before taking dependent actions.
- Parallelize independent reads; keep dependent work sequential.
- Synthesize parallel results before acting.
- If results are empty, partial, or suspiciously narrow, try one or two
  meaningful fallbacks before concluding that no result exists.
- Preserve direct model judgment around approvals, semantic decisions,
  citations, and final validation.

Programmatic Tool Calling is useful for bounded filtering, joining, ranking,
deduplication, batching, aggregation, or deterministic validation over large
intermediate results. Do not choose it merely because calls are numerous or
dependent. State the bounded stage, eligible tools, output schema, retry limit,
stop condition, and handoff back to direct model judgment.

## Grounding and retrieval budgets

Define what needs support, what counts as enough evidence, and how to behave
when evidence is missing. Absence of evidence is not automatically evidence of
absence.

```text
Start with one broad search using short, discriminative keywords. Search again
only when a required fact, owner, date, ID, source, or requested comparison is
missing, a specific artifact must be read, or an important claim would
otherwise be unsupported.
```

For grounded answers:

- cite only retrieved sources and attach citations to supported claims;
- label inference separately;
- state conflicts between sources;
- narrow the answer or report missing evidence instead of guessing.

For creative drafting, distinguish sourced facts from creative wording. Do not
invent names, metrics, dates, roadmap status, customer outcomes, or product
capabilities.

## Long-running workflows and state

For multi-step or tool-heavy tasks, use a short visible preamble before the
first tool call and sparse, outcome-based updates at major phase changes. Do not
narrate routine calls.

Preserve assistant phase values when replaying history. Compact after major
milestones instead of every turn, keep the prompt functionally consistent after
compaction, and do not treat persisted reasoning as always beneficial when the
objective or assumptions have changed.

Keep reusable prompt prefixes stable when prompt caching matters. Add explicit
cache controls only when measured behavior justifies them.

## Reasoning effort

Establish a baseline before changing effort:

- preserve the current GPT-5.5 or GPT-5.4 setting for the first comparison;
- test the same setting and one level lower on representative tasks;
- use lower effort for latency-sensitive work only when quality holds;
- use higher efforts or Pro mode only when evals show a meaningful gain;
- do not recommend the highest setting globally.

Before increasing effort, check for a missing success criterion, dependency
rule, tool-routing rule, or verification loop. Verify live endpoint guidance
when the existing effort was omitted rather than assuming a default.

## Frontend and visual tasks

GPT-5.6 has stronger layout, visual hierarchy, and design judgment, but prompts
should still provide product context, preserve the existing design system, and
name important states and constraints.

- inspect and preserve existing design tokens, components, and patterns;
- do not add unrequested features or decorative UI;
- preserve responsive behavior and expected states;
- render and inspect the result before finalizing.

Choose image detail intentionally for vision work. With GPT-5.6, `original` or
`auto` detail preserves the source image dimensions; large images may increase
input tokens and latency.

## Check work before finishing

Give GPT-5.6 access to validation tools and state what evidence matters.

For coding, run targeted tests, type or lint checks when applicable, affected
builds, and a minimal smoke test when broader validation is too expensive. If a
check cannot run, report why and name the next best evidence.

For visual artifacts, render and inspect layout, clipping, spacing, missing
content, and consistency. For implementation plans, include requirements,
named resources, data or state flow, validation, failure behavior, privacy or
security constraints, and material open questions.

## Suggested prompt structure

Use this structure as a starting point for complex prompts. Keep each section
short and add detail only where it changes behavior.

```text
Role: [the model's function and context]

Personality: [tone and collaboration style]

Goal: [user-visible outcome]

Success criteria: [what must be true before the final answer]

Constraints: [policy, safety, business, evidence, and side-effect limits]

Tools: [which tools to use, when, and what not to use]

Output: [sections, length, format, and tone]

Stop rules: [when to retry, fallback, abstain, ask, or stop]
```

## Prompt migration workflow

1. Switch the model and preserve the current reasoning effort.
2. Run representative evals before changing the prompt.
3. Remove obsolete scaffolding, repeated instructions, irrelevant examples,
   and irrelevant tools one group at a time.
4. Add only the smallest targeted instruction that fixes a measured regression.
5. Rerun the same evals after each prompt or reasoning change.

Do not rewrite a working prompt stack all at once. Otherwise it becomes
impossible to separate model, reasoning, prompt, tool-set, and runtime effects.
