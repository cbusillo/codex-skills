---
name: "openai-docs"
description: "Use when the user asks how to build with OpenAI products or APIs and needs up-to-date official documentation with citations, help choosing the latest model for a use case, or model upgrade and prompt-upgrade guidance; supersedes docs-lookup for OpenAI-specific questions; prioritize OpenAI docs MCP tools, use bundled references only as helper context, and restrict any fallback browsing to official OpenAI domains."
metadata:
  short-description: Reference official OpenAI docs
resources:
  - path: scripts/resolve-latest-model-info.js
    kind: script
    description: Resolve latest model, migration guide, and prompting guide metadata from official docs markdown.
  - path: references/latest-model.md
    kind: reference
    description: Bundled fallback for latest/current model-selection guidance.
  - path: references/upgrade-guide.md
    kind: reference
    description: Bundled fallback for model upgrade planning and migration guidance.
  - path: references/prompting-guide.md
    kind: reference
    description: Bundled fallback for prompt rewrite and prompt-behavior upgrade guidance.
commands:
  - name: resolve-latest-model-info
    source: skill
    resource_path: scripts/resolve-latest-model-info.js
    example_argv: ["node", "scripts/resolve-latest-model-info.js"]
    purpose: Resolves latest model and related guide URLs from official docs markdown.
---


# OpenAI Docs

Provide authoritative, current guidance from OpenAI developer docs. Use the
developer-docs MCP tools first when they are available and useful; use official
OpenAI-domain web search as the fallback. This skill owns OpenAI model
selection, model migration, and prompt-upgrade guidance as well as general API
docs lookup.

## Source priority

- Search with a compact, title-like query of 2-6 discriminative terms instead
  of turning the full user question into a keyword list.
- Use `mcp__openaiDeveloperDocs__search_openai_docs` to discover relevant pages
  and `mcp__openaiDeveloperDocs__fetch_openai_doc` to read exact sections.
- For API schema, parameter, or required-field questions, use
  `mcp__openaiDeveloperDocs__get_openapi_spec` when available alongside the
  relevant guide or reference page.
- When an official page URL is already known, fetch that page before relying on
  search-result summaries.
- Use `mcp__openaiDeveloperDocs__list_openai_docs` only when no clear query or
  candidate page exists.
- If MCP tools are unavailable or unhelpful, continue with official OpenAI web
  sources. Do not install or reconfigure MCP as a side effect of a docs lookup;
  offer setup only when the user asks to configure it.

## Latest-model route

- Fetch `https://developers.openai.com/api/docs/guides/latest-model.md` first
  for latest/current/default model questions.
- For a latest/current/default migration, run
  `node scripts/resolve-latest-model-info.js`, then fetch the returned migration
  and prompting URLs.
- Treat a non-2xx, empty, whitespace-only, or non-substantive guide response as
  unavailable. Try MCP fetch/search or official OpenAI web search for the same
  guidance before using bundled fallbacks.
- Preserve explicit targets. If the user asks for GPT-5.4, do not silently
  retarget the work to GPT-5.6; mention newer guidance only as optional context.
- If current remote guidance cannot be read, use the bundled references and
  disclose that fallback guidance was used.
- If current OpenAI pages disagree, state the conflict and avoid inventing a
  single value.

## Workflow

1. Classify the request as general docs lookup, model selection, model
   migration, prompt migration, or broader API/provider implementation work.
2. For model migrations, inventory active model usage, reasoning settings,
   adjacent prompts, routers, fallbacks, schemas, parsers, tools, and tests.
3. Map workload roles rather than replacing every model with the flagship tier.
   Preserve historical docs, examples, eval baselines, comparison code,
   intentionally pinned fallbacks, and ambiguous usage unless explicitly asked
   to change them.
4. For GPT-5.5 or GPT-5.4 migrations, preserve the current reasoning effort for
   the baseline and test the same setting plus one level lower. Do not guess an
   omitted setting when current docs or host behavior are unclear.
5. Switch the model and run representative evals before rewriting prompts.
   Remove redundant or stale scaffolding one group at a time and add only the
   smallest instruction needed for a measured regression.
6. Keep optional capabilities such as Pro mode, explicit caching, persisted
   reasoning, Programmatic Tool Calling, and multi-agent behavior separate from
   the baseline migration unless the user explicitly requests them.
7. Do not turn a model-and-prompt upgrade into an SDK, endpoint, provider,
   tooling, IDE, plugin, shell, auth, tool-schema, parser, or orchestration
   migration without explicit scope.
8. Validate user-visible behavior and machine-readable contracts. Report what
   changed, what remained pinned, what was not run, and any compatibility
   blockers.

## Reference map

Read only what you need:

- `https://developers.openai.com/api/docs/guides/latest-model.md` -> current model-selection and "best/latest/current model" questions.
- `references/latest-model.md` -> bundled fallback for model-selection and "best/latest/current model" questions.
- `references/upgrade-guide.md` -> bundled fallback for model upgrade and upgrade-planning requests.
- `references/prompting-guide.md` -> bundled fallback for prompt rewrites and prompt-behavior upgrades.

## Quality rules

- Treat OpenAI docs as the source of truth; avoid speculation.
- Keep migration changes narrow and behavior-preserving.
- Prefer prompt-only fixes for prompt-specific regressions; do not rewrite a
  working prompt stack wholesale.
- Do not invent pricing, availability, parameters, API changes, or breaking changes.
- Keep quotes short and within policy limits; prefer paraphrase with citations.
- If multiple pages differ, call out the difference and cite both.
- If official docs and repo behavior disagree, state the conflict and stop before making broad edits.
- If docs do not cover the user’s need, say so and offer next steps.

## Tooling notes

- Prefer MCP doc tools for OpenAI markdown docs when available, then use
  official-domain web search when MCP is unavailable or unhelpful.
- When falling back to web search, restrict to official OpenAI domains (developers.openai.com, platform.openai.com) and cite sources.
