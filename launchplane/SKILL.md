---
name: launchplane
description: Unified Launchplane Expert for inspecting runtime context and performing operator mutations. Use to orient on product state, manage secrets, sync runtime config, and orchestrate deployments.
---

# Launchplane Expert

Use this skill to inspect product/runtime state and perform safe,
authenticated mutations via the Launchplane service or CLI.

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
- **Workflow**:
  1. Inspect Context to identify the target and change needed.
  2. Build a product-config request.
  3. **Dry-run** and inspect redacted results.
  4. **Apply** with a concrete reason.

## Intentionality & Safety

This skill combines inspection and mutation. You must explicitly announce when
you are transitioning from **Inspecting Context** to **Executing Operator
Actions**. Never apply a mutation without a preceding dry-run and situational
verification.

## Tools

- `scripts/launchplane-context.py`: Structural state helper.
- `launchplane` CLI: Primary operator tool.
