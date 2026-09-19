---
name: security-review
description: Use only when the user explicitly requests security work such as a security review, audit, threat model, secure-by-default guidance, auth/authorization review, secrets check, tenant isolation review, webhook safety review, supply-chain/release risk review, or asks whether code is safe from a security perspective. Do not trigger for ordinary code review, debugging, readiness checks, or non-security implementation work.
metadata:
  short-description: Review security and threat models
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

## Security Focus Areas

When reviewing specific technologies or project types, use the relevant focus
areas in `references/focus-areas.md` as guidance. This includes Odoo, Next.js,
Launchplane, and infrastructure components.

### Repository-Specific Guidance

Before applying generic focus areas, check for repository-specific security
documentation and policies:

1. **`.github/github.json`**: Check the `docs` block for handles
   like `secrets`, `architecture`, or `policies`. Use these paths to find the
   repo's primary security contracts.
2. **`AGENTS.md`**: Look for security guardrails and ownership boundaries
   specific to the current repository.
3. **Repo Docs**: Read linked security policies (e.g., `docs/secrets.md` or
   `docs/policies/security.md`) to ground the review in the project's established
   safety standards.

Always prioritize the repository's own security documentation over the generic
focus areas.

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
  use the `[docs].local_infra` value from `$CODE_HOME/local-context.toml`,
  falling back to `$CODEX_HOME/local-context.toml` and then
  `~/.code/local-context.toml`, as the local docs source when configured, and
  start read-only unless the user explicitly approves mutation.

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
