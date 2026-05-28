# Docs Lookup Routing

Use this routing guide after checking local repo instructions and metadata.
Prefer agent-facing sources (`AGENTS.md`, `.github/github.json`, and routed docs)
over human-facing README files, but still read README files as a normal local
fallback when they are the only repo-owned source for installation, usage,
product behavior, operational commands, or architecture context. If README is
carrying agent-facing workflow rules, create or recommend a repo follow-up to
move that guidance into `AGENTS.md` or workflow metadata.

## Technology Routes

- **OpenAI / Codex / ChatGPT**: use `openai-docs`; it supersedes this general
  docs skill for OpenAI-specific questions.
- **Code / Codex CLI / Every Code harness behavior**: inspect the local `code`
  repo first for CLI, TUI, sandboxing, browser control, agent orchestration,
  patch validation, and local runtime behavior. Check related local integration
  repos such as `jetbrains-inspection-api` when the task touches those systems.
- **Odoo**: use official Odoo docs for framework behavior and local repo docs
  for tenant/module conventions.
- **JetBrains APIs and inspections**: prefer JetBrains official docs and local
  repo inspection metadata.
- **Next.js, React, Prisma, Mantine, and frontend frameworks**: use official
  framework docs and compare version guidance against local manifests.
- **Docker, GitHub Actions, uv, PyPI, and packaging**: use official docs,
  workflow files, `pyproject.toml`, lockfiles, and repo release policies.
- **Dokploy and Launchplane**: use official or repo-owned operational docs;
  never copy private hosts, tokens, provider payloads, or secret values into
  public notes.
- **Stripe, Shopify, RepairShopr, Fishbowl, and other integrations**: use
  official API docs and local webhook/sync contracts.

## Local Infrastructure Docs

For questions whose correct answer depends on how this specific private/local
environment is configured or accessed, read `~/.code/local-context.toml` and
use `[docs].local_infra` as the local docs path. Route by source of truth, not
provider name: public docs answer generic behavior, while local context answers
this environment's setup, access path, or operator source of truth. Treat the
local docs path as private local context:

- start read-only unless the user explicitly approves mutation
- do not copy private hostnames, paths, secrets, or topology into public issues,
  PRs, docs, or handoffs
- prefer summaries and safe source references over raw command output

If the file or key is missing and local infrastructure context is required, say
that the local docs source is not configured instead of guessing. Do not fall
back to shell environment variables or repo `.env` files for this routing.

When local operational context is missing, stale, misleading, or newly changed,
route that discovery back to the configured local information source instead of
leaving it only in chat. Keep public skills and public GitHub issues free of
private facts; at most, record that the local information source needs a durable
update or capture point.
