---
name: launchplane
description: Unified Launchplane Expert for inspecting runtime context and performing operator mutations. Use to orient on product state, manage secrets, sync runtime config, and orchestrate deployments.
---

# Launchplane Expert

Use this skill to inspect product/runtime state and perform safe,
authenticated mutations via the Launchplane service API.

## Core Goal

Provide situational awareness and safe runtime management. Always favor
service-backed audit trails over local ad-hoc fallbacks.

## Situational Awareness (Context)

Use the context helper to identify product mapping, deploy evidence, and
readiness.

- **Usage**: `uv run scripts/launchplane-context.py --repo OWNER/REPO`
- **Output**: See `references/context.available.example.json` for schema.
- **Reporting**: Report readiness, blockers, and next action based on context.
- **Contract**: See `references/context-helper-contract.md` for config,
  fallback, and redaction behavior.

## Runtime Management (Operator)

Mutate runtime environments, managed secrets, and product config.

- **Safety**: Strictly follow the `references/operator-contract.md`.
- **Auth**: Source local operator credentials through the operator contract;
  do not paste token values into chat, issues, PRs, docs, or logs.
- **First Shot**: For product-config/runtime/secret sync, use the service API
  path from the operator contract first. Do not start by searching for a local
  `launchplane` binary or by poking provider config directly.
- **Workflow**:
  1. Inspect Context to identify the target and change needed.
  2. Source local operator credentials from
     `~/.config/launchplane/local-operator.env`.
  3. Build a product-config request for `POST /v1/product-config/apply`.
  4. **Dry-run** and inspect redacted results.
  5. **Apply** with a concrete reason only after the dry-run succeeds.
  6. Inspect returned `next_actions` and complete required follow-up actions;
     product-config apply can update Launchplane records before the live target
     runtime has been synced.

## Intentionality & Safety

This skill combines inspection and mutation. You must explicitly announce when
you are transitioning from **Inspecting Context** to **Executing Operator
Actions**. Never apply a mutation without a preceding dry-run and situational
verification.

## Tools

- `scripts/launchplane-context.py`: Structural state helper.
- `POST /v1/product-config/apply`: Primary product-config operator path for
  trusted local agents; use local-operator bearer auth and dry-run before apply.
- Launchplane host-only CLI helpers: Use only when you are explicitly on the
  Launchplane host via SSH or the repo provides a concrete command. Do not
  assume a global `launchplane` binary exists on ordinary workstations.
