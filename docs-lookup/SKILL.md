---
name: docs-lookup
description: Use when the answer depends on external docs or environment-specific operational context rather than local repo code alone; includes discovering source-of-truth docs and access paths for private operations such as DNS or Cloudflare records, but not performing infrastructure actions or mutations.
metadata:
  short-description: Find external docs and ops context
---

# Docs Lookup

Use this skill when correctness depends on current documentation or routed
operational context rather than training memory. This is especially important
for APIs, SDKs, CLIs, frameworks, cloud services, deployment platforms, package
managers, access paths, and version-specific behavior.

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
- tasks that ask where to make private DNS or Cloudflare changes, such as
  verification records, zone ownership, token location, or provider access
  paths.
- tasks where the answer depends on environment-specific operational context or
  discovering source-of-truth docs or access paths for private operations.

Do not use this skill for stable local repo facts that can be answered directly
from checked-in code or docs.

Do not use this skill to perform infrastructure actions, API mutations,
operator workflows, rollback/snapshot decisions, or production-impacting
changes. Use the owning operator skill such as `infra-ops` or `launchplane`
after docs and authority are discovered.

## Source Order

Prefer sources in this order:

1. Configured local operational context when the task depends on this specific
   private environment's setup, access path, or source of truth.
2. Local repo docs and source for project-specific behavior.
3. Official product or project docs.
4. Official API references, release notes, migration guides, changelogs, and
   source repositories.
5. Package registry pages only for package metadata or version facts.
6. Trusted community sources only when official docs are missing, clearly
   incomplete, or the user explicitly wants ecosystem practice.

For OpenAI products and APIs, use `openai-docs`; it supersedes this general
docs workflow for OpenAI-specific questions.

For provider or infrastructure tasks, route by source of truth rather than
provider name. Use official docs for generic behavior, local context routing for
this environment's setup or access path, and the owning operator skill for
read-only inventory, managed product/runtime state, production checks, or
mutations.

This skill may identify the source-of-truth route and relevant docs. Once the
request needs live tenant/account identity, current record or runtime inventory,
health evidence, production status, or any mutation, switch to the owning
operator skill such as `infra-ops` or `launchplane`.

## Local Operational Context

Use local operational context when the task is about how this particular
environment is configured, reached, mutated, verified, or owned. Do not infer
that a product repo, cloud provider, dashboard, deployment platform, or browser
session is the source of truth until the configured local context route has been
checked or ruled out.

For private DNS or Cloudflare requests, such as adding a verification CNAME or
TXT record or finding where provider access is configured, use this skill only
to find the configured local infrastructure source of truth. Then hand live
record inspection or mutation to `infra-ops`. Do not search product repo `.env`
files, shell history, or common token locations as a first move.

Keep the skill guidance conceptual. Do not add local service names, hostnames,
tokens, account details, topology, or private repo inventories to public skills.
Those facts belong in the configured local information source.

If a session discovers that local operational context is missing, stale,
misleading, or newly changed, do not leave that discovery only in chat. Route a
durable capture back to the configured local information source. Start read-only
unless the user has approved mutation; if updating that source is not approved,
record the need as a private-safe follow-up without copying private facts into
public issues, PRs, docs, or summaries.

## Workflow

1. Identify the exact technology, version, language, and task. If the version is
   missing and matters, inspect local manifests first (`package.json`,
   `pyproject.toml`, `uv.lock`, `Cargo.toml`, `go.mod`, Dockerfiles, CI files).
2. If the task depends on private/local operational state, read the configured
   local context route before following provider-specific or product-repo clues.
   If the task asks you to inspect, operate, validate, or mutate infrastructure,
   switch to the owning operator skill after identifying the docs/access path.
3. Search current docs. Use primary sources; when searching the web, use precise
   queries and official-domain filters where possible.
4. Fetch the specific page or section needed. Avoid broad summaries when a
   reference page, migration note, or release note answers the question.
5. Compare docs against local code before editing. If docs and repo behavior
   disagree, call out the mismatch and avoid broad changes until the intended
   contract is clear.
6. Answer or implement narrowly using the sourced behavior.
7. Cite the sources used in the response when the user asked for an answer,
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
