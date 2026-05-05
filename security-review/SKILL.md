---
name: security-review
description: Use only when the user explicitly requests security work such as a security review, audit, threat model, secure-by-default guidance, auth/authorization review, secrets check, tenant isolation review, webhook safety review, supply-chain/release risk review, or asks whether code is safe from a security perspective. Do not trigger for ordinary code review, debugging, readiness checks, or non-security implementation work.
---

# Security Review

Use this skill for explicit security work. Keep reviews grounded in repository
evidence and realistic attacker paths; avoid generic checklist reports.

## Modes

Choose the narrowest mode that satisfies the request:

- Secure implementation guidance: write or modify code with secure defaults.
- Focused security review: inspect a change, file, PR, workflow, or feature for
  likely vulnerabilities.
- Threat model: map assets, trust boundaries, entry points, attacker
  capabilities, abuse paths, existing controls, and mitigations.
- Secrets/supply-chain review: inspect env handling, CI, packaging, release,
  Docker builds, tokens, trusted publishing, and dependency flows.

## Workflow

1. Define scope: repo, branch/PR, files, feature, deployment surface, and what
   kind of security answer the user wants.
2. Identify languages, frameworks, runtime model, auth model, data stores,
   external integrations, and exposed entry points from local evidence.
3. Read repo-specific instructions and docs before applying generic advice.
4. Inspect code paths that cross trust boundaries: public endpoints, parsers,
   uploads, webhooks, auth/session handling, database access, admin paths,
   background jobs, CI/release workflows, secrets, and deployment config.
5. Prioritize findings by realistic impact and likelihood. Do not inflate risk
   without a plausible attacker path.
6. For each finding, include file/line evidence, impact, affected asset or
   boundary, and a concrete mitigation.
7. When asked to fix issues, fix one coherent finding at a time and run the
   relevant repo gate or targeted test. Avoid breaking existing behavior with a
   theoretical hardening change.

## Report Shape

Lead with findings, ordered by severity. Use this format for each issue:

- Severity: critical, high, medium, low, or informational.
- Evidence: file path and line number when code-backed.
- Impact: one concise sentence.
- Abuse path: how an attacker or misuse path reaches the issue.
- Mitigation: concrete code/config/process change.

If there are no findings, say so clearly and mention residual scope limits or
untested surfaces.

## Threat Model Shape

For threat models, keep it concise and repo-grounded:

- Scope and assumptions.
- Components and entry points.
- Assets and trust boundaries.
- Realistic attacker capabilities and non-capabilities.
- Top abuse paths with likelihood, impact, and existing controls.
- Recommended mitigations and open questions.

Ask 1-3 targeted questions before a final threat model when deployment model,
internet exposure, auth expectations, or data sensitivity materially change the
risk ranking. If the user wants a quick pass, state assumptions instead of
blocking.

## Repo-Specific Focus

### Odoo repos

- ACLs, record rules, group checks, multi-company/tenant boundaries.
- `sudo`, `with_user`, context flags, computed fields, import/export paths.
- Public controllers/routes, portal flows, JSON endpoints, file handling.
- Secrets in `ir.config_parameter`, env files, logs, fixtures, migrations, and
  generated configs.
- Shopify/RepairShopr/Fishbowl or other integration webhooks and sync loops.

### VeriReel / Next.js apps

- Auth/session boundaries, password reset, account deletion, owner/admin paths.
- Prisma query authorization and public verification endpoints.
- Billing/subscription state, Stripe or payment webhooks, raw-body validation,
  replay handling, idempotency, and customer/account mapping.
- QR/public resource identifiers and enumeration risk.

### Launchplane / deployment control plane

- Secret records, runtime environment authority, promotion/deploy records,
  backup gates, restore flows, and fail-closed behavior.
- Separation between deploy/operator credentials and app/runtime credentials.
- Auditability of promotion, deploy, backup, and secret changes.

### Docker image repos

- Build secrets, token leakage in layers/logs, source injection, image contents,
  base image trust, entrypoint behavior, user permissions, and downstream image
  contracts.
- Verify with real image inspection when Dockerfile reasoning is not enough.

### Python packages and CLIs

- PyPI trusted publishing, release tags, workflow permissions, token handling,
  dependency pinning/locking, archive contents, and live-credential tests.
- Public API clients should not require real credentials for normal tests.

### GitHub and agent workflows

- GitHub Actions permissions, PAT handling, bot identity, branch protection,
  deploy labels, PR comment automation, and generated artifact leakage.
- Do not put bot tokens, `.env` files, private host notes, or credentials in
  repo config or issue/PR bodies.

### Local infrastructure

- For local Mac, home-lab, network, Proxmox/LXC, backup, or service security,
  use `claude-local-machine` docs as the local source of truth and start
  read-only unless the user explicitly approves mutation.

## Guardrails

- Do not report lack of TLS/HSTS as a finding for local/dev-only paths unless
  production exposure is in scope and the deployment contract requires it.
- Do not recommend broad dependency upgrades as a security fix without evidence
  and a compatibility plan.
- Do not expose secrets in reports. Redact values and cite only safe key names,
  file paths, or config surfaces.
- Do not create a report file in the repo unless the user asks for an artifact;
  chat reports are usually enough for focused reviews.
- If external security guidance may have changed, use `docs-lookup` and prefer
  official references.
