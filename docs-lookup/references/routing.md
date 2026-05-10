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

For local Mac, home-lab, network, Proxmox/LXC, backup, or private service
questions, use the path configured in `LOCAL_INFRA_DOCS` when present. Treat it
as private local context:

- start read-only unless the user explicitly approves mutation
- do not copy private hostnames, paths, secrets, or topology into public issues,
  PRs, docs, or handoffs
- prefer summaries and safe source references over raw command output

If `LOCAL_INFRA_DOCS` is missing and local infrastructure context is required,
say that the local docs source is not configured instead of guessing.
