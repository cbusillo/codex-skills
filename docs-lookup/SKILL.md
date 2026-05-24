---
name: docs-lookup
description: Use when a task depends on current documentation, API references, SDK behavior, framework configuration, CLI usage, version migrations, or external service integration details. Prefer primary/official sources and cite the docs used instead of relying on memory for unstable API or product facts.
---

# Docs Lookup

Use this skill when correctness depends on current documentation rather than
training memory. This is especially important for APIs, SDKs, CLIs, frameworks,
cloud services, deployment platforms, package managers, and version-specific
behavior.

## Trigger Examples

Use this skill for:

- "how do I use <library/API/framework>"
- latest/current/default behavior, model, parameter, config, or migration docs
- SDK method names, request/response shapes, auth setup, webhooks, billing,
  deployment, package publishing, or CI configuration
- bug fixes where a library, provider, or CLI may have changed behavior
- tasks that involve fast-moving ecosystems where the agent's internal training
  data may be stale or imprecise.
- tasks that mention Odoo, JetBrains APIs, Next.js, React, Prisma, Mantine,
  Docker, GitHub Actions, uv, PyPI, Dokploy, Launchplane, Stripe, Shopify,
  RepairShopr, Fishbowl, or similar tools.
- tasks involving local/private infrastructure docs; use the local context
  routing from `references/routing.md` when configured.

Do not use this skill for stable local repo facts that can be answered directly
from checked-in code or docs.

## Source Order

Prefer sources in this order:

1. Local repo docs and source for project-specific behavior.
2. Official product or project docs.
3. Official API references, release notes, migration guides, changelogs, and
   source repositories.
4. Package registry pages only for package metadata or version facts.
5. Trusted community sources only when official docs are missing, clearly
   incomplete, or the user explicitly wants ecosystem practice.

For OpenAI products and APIs, use `openai-docs`; it supersedes this general
docs workflow for OpenAI-specific questions.

## Workflow

1. Identify the exact technology, version, language, and task. If the version is
   missing and matters, inspect local manifests first (`package.json`,
   `pyproject.toml`, `uv.lock`, `Cargo.toml`, `go.mod`, Dockerfiles, CI files).
2. Search current docs. Use primary sources; when searching the web, use precise
   queries and official-domain filters where possible.
3. Fetch the specific page or section needed. Avoid broad summaries when a
   reference page, migration note, or release note answers the question.
4. Compare docs against local code before editing. If docs and repo behavior
   disagree, call out the mismatch and avoid broad changes until the intended
   contract is clear.
5. Answer or implement narrowly using the sourced behavior.
6. Cite the sources used in the response when the user asked for an answer,
   when the fact is unstable, or when source attribution will help future work.

## Optional Docs CLIs

If a documentation CLI is available and fits the task, it may be used as a
source-finding helper, but do not make it the only path.

- Context7/ctx7: useful for library and framework docs.
- chub/Context Hub: useful for third-party API and SDK docs.
- Vendor MCP docs tools: use them when they are installed and official.

Do not install a global docs CLI just to answer a small question unless the user
asked for it or the task will clearly benefit. If a docs CLI query could include
private code, credentials, customer data, or proprietary architecture, rewrite
the query to remove sensitive details.

## Repo Routing

If the current repo has `.github/github.json` with a `docs` block,
use those repo-relative paths as the primary local routing targets before
falling back to repo-root search. Prefer `docs.index` as the entry point, then
relevant semantic paths such as `docs.architecture`, `docs.operations`,
`docs.style`, or `docs.policies`.

Always check the repository's `AGENTS.md` before using external docs for
local-specific architecture or operational questions. Treat README files as
human-facing by default, but keep them as a normal local fallback when repo
metadata and `AGENTS.md` do not cover the needed operational or architecture
context. When README carries agent-operational guidance that is not captured in
`AGENTS.md` or repo metadata, note that as a repo-docs follow-up instead of
ignoring it.

For technology-specific routing, including optional local infrastructure docs,
see `references/routing.md`.

## Quality Rules

- Do not invent API parameters, model names, config keys, pricing, limits,
  availability, or migration requirements.
- Preserve explicit user targets. If the user asks for a specific version,
  answer for that version and mention newer guidance separately only when useful.
- Prefer short quotes and paraphrase. Keep citations close to the claims they
  support.
- If sources disagree, cite both and explain the difference.
- If docs are unavailable or inconclusive, say so and give the safest next
  verification step.
- Keep sourced changes narrow; do not turn docs lookup into an unrelated
  dependency upgrade or migration.
