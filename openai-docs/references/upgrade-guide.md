# Upgrading to GPT-5.6

Use this offline guide when the user asks to migrate an existing OpenAI API
integration, prompt stack, agent, model router, or model picker to the GPT-5.6
family. Pair it with current OpenAI docs whenever they are available.

The default explicit flagship target is `gpt-5.6-sol`. The `gpt-5.6` alias
currently routes to Sol; use the alias only when the integration intentionally
prefers family aliases.

## Freshness check

Before applying this bundled guide for a latest/current/default upgrade, run
`node scripts/resolve-latest-model-info.js` from the OpenAI Docs skill directory.

- If the command returns `modelSlug: "gpt-5p6-sol"`, use the returned migration
  and prompting URLs as the source of truth and use this file as an offline
  workflow summary.
- If it returns a different model, fetch the returned guides and do not silently
  retarget the request to GPT-5.6.
- If the remote guidance is unavailable, use this fallback and disclose that
  the freshness check could not be completed.
- If the user explicitly named a target model, preserve that target and use
  current docs only for compatibility checks and caveats.

## Core principle

Do not perform a blind model-string replacement. Preserve the workload's role,
behavior, latency and cost class, reasoning level, endpoint contract, tool
semantics, cache behavior, output shape, and downstream parser expectations.
Then make the smallest safe migration.

Do not adopt Pro mode, persisted reasoning, explicit caching, Programmatic Tool
Calling, or multi-agent behavior as an incidental part of a model upgrade.
Evaluate optional capabilities separately so their effects remain measurable.

## Inventory before editing

Search for more than literal model IDs:

- model strings, aliases, environment variables, CLI flags, config defaults,
  deployment settings, registries, allowlists, and UI model pickers;
- Responses, Chat Completions, Batch, and provider-adapter calls;
- reasoning settings, token budgets, sampling settings, and timeouts;
- tool schemas, structured outputs, parsers, replay logic, cache fields, and
  multimodal detail settings;
- system, developer, user, and tool-description prompts tied to each usage;
- tests, snapshots, examples, pricing metadata, capability maps, eval baselines,
  and comparison code.

Classify every result as active production usage, intentional fallback, router
entry, test or eval fixture, historical documentation, or ambiguous. Do not
upgrade historical, comparison, fixture, or ambiguous usages unless the user
explicitly asks for them.

## Choose the target by workload role

| Existing role | Starting GPT-5.6 target |
| --- | --- |
| Flagship GPT-5, GPT-5.5, GPT-5.4, hardest coding, or quality-first reasoning | `gpt-5.6-sol` |
| GPT-5.5 Pro or another old Pro route | `gpt-5.6-sol` plus `reasoning.mode: "pro"` when Pro behavior must be preserved; never invent a `gpt-5.6-pro` slug |
| Mini-like, balanced lower-cost, or medium-throughput worker | `gpt-5.6-terra` |
| Nano-like, classification, extraction, routing, strict-latency, or high-volume worker | `gpt-5.6-luna` |
| Router, fallback chain, or model picker | Add or map each tier by role; do not collapse everything into Sol |
| Third-party or provider-specific model | Leave unchanged unless provider migration is explicitly requested |

Preserve existing model entries by default. Add GPT-5.6 family options rather
than deleting older models unless the user explicitly requests replacement or
cleanup. Do not invent pricing, limits, capability flags, or context windows;
verify them in current docs before updating registries or UI metadata.

## Preserve reasoning before tuning

For GPT-5.5 or GPT-5.4 migrations:

1. Preserve the current explicit reasoning effort for the first GPT-5.6 run
   when the target supports it.
2. If the old effective setting was omitted or is unknown, do not guess. Verify
   current endpoint behavior or flag the site for comparison.
3. Run the baseline at the same setting and test one level lower on
   representative tasks.
4. Increase effort only when evals show a meaningful quality gain.
5. Reserve the highest efforts and Pro mode for measured quality-first cases;
   do not recommend them globally.

Before increasing effort, check whether the real failure is a missing success
criterion, dependency rule, tool-routing rule, state-replay bug, or validation
loop. Current guidance says omitted GPT-5.6 effort defaults to `medium`, but
preserving an explicit old setting remains safer for controlled comparisons.

### Endpoint and tool compatibility gate

Record the old endpoint and effective effort before classifying a migration as
model-and-prompt-only. An omitted old value that behaved as `none` is not
preserved by omitting GPT-5.6 effort, because GPT-5.6 defaults to `medium`.

For Chat Completions routes that use function tools, preserve effective `none`
explicitly with `reasoning_effort: "none"`. If the workflow requires reasoning
and function tools together, moving it to Responses is a compatibility migration,
not an incidental model-string change. Verify SDK and endpoint support before
editing the request shape.

## Classify the migration

### Model and prompt only

Use this path when the endpoint, request shape, tools, schemas, parsers, cache,
state handling, and multimodal behavior can remain unchanged.

- change the active model target by workload role;
- preserve the current prompt for the first comparison;
- make only prompt edits tied to measured failures;
- validate the same user-visible and machine-readable contracts.

### Compatibility migration

Use this classification when a safe migration needs parameter, endpoint,
cache, state, tool-loop, structured-output, parser, or multimodal-detail changes.
Make those changes only when implementation work is inside the user's requested
scope. Otherwise report the exact blocker and smallest follow-up task.

### Leave unchanged

Leave historical examples, snapshots, eval baselines, comparison code,
intentionally pinned fallbacks, unsupported providers, and ambiguous usages
unchanged. List them explicitly so they are not mistaken for missed work.

## Prompt migration judgment

Run representative traces before editing prompts. For GPT-5.6, prefer:

- shorter, outcome-oriented instructions;
- explicit success criteria, dependencies, stopping conditions, and completion
  boundaries;
- preserved user-provided values and decision criteria for implicit choices;
- explicit autonomy, approval, evidence, and tool-routing boundaries;
- sparse, outcome-based progress updates for long-running work;
- real validation before declaring completion.

Remove repeated rules, stale model-specific scaffolding, irrelevant examples,
and contradictory instructions. Avoid generic requests to be brief, thorough,
or to think step by step when they do not address a measured failure.

## Scope boundaries

This guide may update model strings, directly related prompts, and configuration
or registry entries needed to preserve the existing model role. It may inspect
adjacent code, schemas, tools, caches, state handling, and tests to determine
whether the migration is safe.

Do not silently turn a model-and-prompt upgrade into an SDK migration, endpoint
migration, provider migration, orchestration rewrite, tool-schema redesign,
parser rewrite, cache redesign, or optional-feature rollout. Treat those as
separate implementation work or explicit blockers.

## Upgrade workflow

1. Fetch current model and prompting guidance.
2. Inventory active usage, prompts, configs, registries, parsers, and tests.
3. Classify each usage by role and migration class.
4. Choose Sol, Terra, Luna, unchanged, or confirmation-needed.
5. Preserve the old effective reasoning behavior for the baseline.
6. Check endpoint support, especially Chat Completions plus function tools.
7. Apply the smallest safe model, prompt, and directly related metadata changes.
8. Keep optional GPT-5.6 capabilities separate from the baseline migration.
9. Run existing tests and representative evals.
10. Report changed, unchanged, blocked, and confirmation-needed sites separately.

## Validation matrix

Prefer a controlled comparison:

1. old model + old prompt + old settings;
2. GPT-5.6 target + same prompt + preserved reasoning effort;
3. GPT-5.6 target + same prompt + one lower effort;
4. GPT-5.6 target + the smallest prompt or compatibility fix required by a
   measured failure;
5. any optional feature treatment, isolated from the baseline.

Measure the contracts that matter: task success, completeness, output validity,
parser success, tool choice and arguments, retries, completion rate, latency,
token and cache use, cost per successful task, citations, preserved behavior,
and validation evidence.

For routers and pickers, test representative work for every mapped role. Verify
that Luna or Terra is not accidentally used for quality-critical work and that
Sol is not accidentally used for every workload.

## Required final report

Return:

- `Current usage inventory`
- `Target mapping`
- `Changes made`
- `Compatibility checks`
- `Prompt changes`
- `Validation`
- `Unchanged sites`
- `Blockers and open questions`

Do not call the migration complete merely because model strings changed. It is
complete only when affected behavior and contracts have been validated or the
remaining gaps are stated explicitly.
