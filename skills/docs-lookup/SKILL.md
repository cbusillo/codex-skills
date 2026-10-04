---
name: docs-lookup
description: Use when the answer depends on external docs or environment-specific operational context rather than local repo code alone; includes discovering source-of-truth docs and access paths for private operations such as DNS or Cloudflare records, and finding where a credential, API token, or secret is stored, but not performing infrastructure actions or mutations.
metadata:
  short-description: Find external docs and ops context
---

# Docs Lookup

Find current documentation or environment-specific operational context when
local code alone cannot answer the task. Typical requests involve versioned
APIs, SDKs, frameworks, CLIs, cloud services, deployment, packaging, integration
contracts, private DNS/provider access, or credential locations. Stable
checked-in facts do not need this skill. For OpenAI questions, use
`openai-docs`, which supersedes this workflow.

This skill discovers docs and access paths. Once the request needs live
tenant/account identity, record/runtime inventory, health or production evidence,
rollback/snapshot decisions, admin workflows, or any mutation, switch to the
owning operations skill such as `infra-ops` or `launchplane` after discovering
docs and authority. Route by source of truth, not provider name.

## Find And Use Sources

1. Identify the technology, language, task, and exact version. When a missing
   version matters, inspect manifests, lockfiles, Dockerfiles, or CI first.
   Preserve explicit user version targets; mention newer guidance separately
   only when useful.
2. Check `AGENTS.md` before external docs for local architecture or operations.
   For private operational or credential questions, follow step 3 before
   unrouted product-repo clues or fallback searches. Explicit `docs` routes
   remain authoritative for their declared scope; consult local context for
   operational facts they do not provide.
   Use `.github/github.json`'s `docs` paths as primary repo-local routes:
   `docs.index` first, then relevant semantic paths such as architecture,
   operations, style, or policies. Fall back to repo-root search and README
   when instructions/metadata do not cover the context. README is a normal
   human-facing source; if it contains agent workflow guidance missing from
   instructions/metadata, record a repo-docs follow-up.
3. Before technology-specific routing or any private/local operational or
   credential discovery, read [the routing guide](references/routing.md).
   For private operational facts missing from explicit repo docs routes,
   check its configured local context before inferring authority from unrouted
   product clues, provider dashboards, deployment platforms, or browser sessions.
   Credential location lookup always uses the configured local credentials index,
   even when repo docs mention token locations.
   When explicit docs routes do not cover them, private DNS/Cloudflare requests
   start there. Never start private operational or credential discovery by
   scanning product `.env` files, shell history, or common token locations. Missing
   required context is a configuration gap to report, not permission to guess.
4. Honor explicit repo documentation routes for their declared scope and the
   credential index-first rule. Use configured local context for missing private
   operational facts. For other lookup, prefer repo docs/source for project
   behavior; official product docs;
   official API references, release/migration notes, changelogs and source;
   registries for package metadata/version facts; trusted community sources
   only when official docs are missing/incomplete or the user wants ecosystem
   practice. Search precisely with official-domain filters where possible and
   fetch the specific section that answers the task.
5. Compare documentation against local code before editing. Call out mismatches
   and avoid broad changes until the intended contract is clear. Answer or
   implement narrowly; do not turn lookup into an unrelated upgrade/migration.
6. Cite sources close to the claims when answering a question, the fact is
   unstable, or attribution will help future work. Prefer paraphrases and short
   quotes. If sources disagree, cite both and explain; if unavailable or
   inconclusive, say so and give the safest next verification step. Never invent
   API parameters, model names, configuration keys, prices, limits, availability, or migration
   requirements.

## Private Context

Keep local service names, hosts, accounts, tokens, topology and private repo
inventories in the configured local information source, not public skills or
GitHub artifacts. Start read-only unless mutation is approved. For credential
requests, follow the routing guide's index-first lookup, declared-key checks,
value handling and missing-entry procedure before accessing a value.

When local context is missing, stale, misleading, or newly changed, route a
durable capture back to that information source. If updating it is not approved,
record a private-safe follow-up without copying private facts into public
issues, PRs, docs, handoffs or summaries. Do not leave the discovery only in chat.

## Optional Docs Tools

Use an available documentation helper when it fits: Context7/ctx7 for libraries
and frameworks, chub/Context Hub for third-party APIs/SDKs, or installed official
vendor MCP tools. Keep other source paths available. Do not install a global
CLI for a small lookup unless requested or clearly beneficial. Rewrite queries
to omit private code, credentials, customer data and proprietary architecture.
