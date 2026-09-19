---
name: "openai-docs"
description: "Use for Codex models/pricing, scheduled tasks, skills, settings, setup, troubleshooting, customization, automations, and self-knowledge—including 'you,' 'your,' 'this app,' or 'this coding agent' when they refer to Codex—and for OpenAI APIs/products and ChatGPT Work. Also use for model choice/migration, prompting, SDKs, Responses, Realtime, agents, evals, and Chat/Work/Codex comparisons. Supersedes docs-lookup for OpenAI-specific questions. Do not use for generic app/software tasks that merely mention Codex."
metadata:
  short-description: "Codex models/pricing, scheduled tasks, skills, settings, setup, troubleshooting, and self-knowledge; OpenAI APIs and ChatGPT Work. 'You'/'this app' means Codex only."
resources:
  - path: scripts/resolve-latest-model-info
    kind: script
    description: Resolve current model and guide URLs with a compatible Node runtime on POSIX.
  - path: scripts/resolve-latest-model-info.cjs
    kind: script
    description: CommonJS resolver entrypoint for Windows and direct Node invocation.
  - path: scripts/fetch-codex-manual.mjs
    kind: script
    description: Fetch and verify the current Codex manual with a temporary cache and outline.
  - path: references/model-migration.md
    kind: reference
    description: Route model migrations and model-specific prompting requests.
  - path: references/model-selection.md
    kind: reference
    description: Resolve current model selection and workload tradeoffs.
  - path: references/official-docs.md
    kind: reference
    description: Look up official product and API documentation.
  - path: references/codex-self-knowledge.md
    kind: reference
    description: Use the Codex manual for broad setup and cross-topic synthesis.
  - path: references/mcp-diagnostics.md
    kind: reference
    description: Configure or diagnose local documentation MCP only when requested.
  - path: references/latest-model.md
    kind: reference
    description: Bundled fallback for current model-selection guidance.
  - path: references/upgrade-guide.md
    kind: reference
    description: Route unavailable live migration guidance to the relevant fallback.
  - path: references/upgrading-to-gpt-6-astra.md
    kind: reference
    description: Scoped Astra migration workflow and offline compatibility guidance.
  - path: references/prompting-guide.md
    kind: reference
    description: Bundled fallback for model-specific prompt guidance.
commands:
  - name: resolve-latest-model-info
    source: skill
    resource_path: scripts/resolve-latest-model-info
    example_argv: ["sh", "scripts/resolve-latest-model-info"]
    purpose: Resolve current model and guide URLs on POSIX.
  - name: resolve-latest-model-info-node
    source: skill
    resource_path: scripts/resolve-latest-model-info.cjs
    example_argv: ["node", "scripts/resolve-latest-model-info.cjs"]
    purpose: Resolve current model and guide URLs with Node.js 18 or newer.
  - name: fetch-codex-manual
    source: skill
    resource_path: scripts/fetch-codex-manual.mjs
    example_argv: ["node", "scripts/fetch-codex-manual.mjs"]
    purpose: Fetch the current Codex manual and outline for broad Codex questions.
---

# OpenAI Docs

Apply [task scope and authorization](../references/execution-scope.md) when
using this workflow; it defines how existing approval and task boundaries apply.

Provide current, cited OpenAI product, API, model, and Codex guidance. Read zero or one primary reference.

**First substantive action:** Search the user's exact requested official OpenAI documentation topic and any explicitly named model using a concise, topic-specific query of 2-6 essential terms. When an already-available direct official documentation search and page-retrieval capability is present, use it first: search, then fetch or open the matching official page before general web search. Otherwise, immediately use official-domain web search, then actually open or fetch the relevant official page. Complete this source order before reading a reference, inspecting local or repository files, running a Codex manual or model resolver, drafting a plan, or answering from memory. Use the actual fetched page, not a search snippet or an unopened link. If one official search or page does not establish the answer, search another appropriate official domain and actually open or fetch the result. Preserve the exact requested model; never substitute a newer model.

**Only exception:** An explicitly requested, genuinely broad, cross-topic Codex setup, orientation, or system-map synthesis may use the manual first when shell execution and an allowed temporary cache are available. A specific Codex feature, setting, command, error, model, or requested citation remains docs-first. Mixed Chat/Work/Codex comparisons are official documentation questions, not manual-first Codex requests.

For generic software tasks, answer the software task directly. OpenAI implementation, debugging, SDK, API, prompting, agent, and eval requests are not generic.

For a straightforward factual or citation-only request, follow the source order and do not read a route reference. This includes straightforward API facts, ChatGPT Work or mixed Chat/Work/Codex comparisons, model tiers, aliases, Pro mode, reasoning settings, factual migration baselines, and narrow Codex facts. Prioritize `learn.chatgpt.com` for ChatGPT Work.

For an instruction audit or prompt-only refactor, fetch the named model's
current prompting guidance and inspect the active instruction sources. Preserve
existing approval and quality policies unless changing them is explicitly in
scope. Identify static conflicts separately from measured model behavior; a
model switch or API evaluation is not a prerequisite for reviewing instruction
files. The model-migration procedure applies only to an actual model migration.

## Choose one primary route

Use the first matching route, and read its reference only when the requested task needs that specialized workflow:

- **Explicitly requested local documentation integration:** Read [integration guidance](references/mcp-diagnostics.md) only when the user explicitly requests that local integration.
- **Model migration, upgrades, or model-specific prompting:** Read [model-migration.md](references/model-migration.md) for actual migration planning, implementation, dynamic target resolution, or prompt changes. Preserve an explicitly requested target.
- **Model selection and comparisons:** Read [model-selection.md](references/model-selection.md) only when nuanced current, latest, default, cost, latency, quality, or modality tradeoffs need more guidance. Do not run a migration resolver for selection alone.
- **Product, API, ChatGPT Work, and mixed Chat/Work/Codex documentation:** Read [official-docs.md](references/official-docs.md) only when fetched official pages leave source selection, API schemas, or the requested implementation unresolved. This route is not manual-first.
- **Explicitly broad Codex setup, orientation, or cross-topic synthesis:** Read [codex-self-knowledge.md](references/codex-self-knowledge.md) when the eligible Codex manual or deeper Codex procedures are needed.

Read at most one primary reference. Do not open every route, bundled model guide, or helper script. Read a supporting reference or run a helper only when the chosen workflow demonstrably needs it.

## Source and execution boundaries

- Search, open, fetch, and cite only `developers.openai.com`, `platform.openai.com`, and `learn.chatgpt.com`. Cite the page that supports the claim. State uncertainty when official sources do not establish pricing, availability, account access, limits, or behavior.
- Preserve an explicitly requested model for selection, migration, and prompting. Resolve an unspecified latest or current migration target only after searching and fetching current official guidance.
- Use `references/latest-model.md` only as a disclosed fallback after current official model guidance does not answer the question. Read `references/upgrading-to-gpt-6-astra.md` only for an actual, requested GPT-6 migration; read `references/prompting-guide.md` only for requested prompting work.
- Before building, running, editing, debugging, or testing an API-backed app or tool, use `openai-platform-api-key` first when available. Documentation, conceptual examples, model selection, and read-only guidance do not require an API key.
- Say "OpenAI Docs" or "official OpenAI documentation" in user-facing answers. Keep exact official citations and examples concise.
