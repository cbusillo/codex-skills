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

Do not treat archived workstation files under `~/.config/launchplane/` as the
authority for current Launchplane runtime or product state. Files such as
`service.env`, `dokploy.env`, and `runtime-environments.toml` can be useful
historical clues, but they are not live records. When a task asks about current
product state, use the deployed Launchplane service/API or operator UI first;
use direct database access only from an explicitly approved host-side context.

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
- **Auth**: Prefer signed-in, scoped operator sessions in the Launchplane UI or
  service API. Source terminal/local operator credentials only through the
  operator contract; do not paste token values into chat, issues, PRs, docs, or
  logs.
- **First Shot**: For product-config/runtime/secret sync, use the service API
  path from the operator contract first. Do not start by searching for a local
  `launchplane` binary or by poking provider config directly.
- **Workflow**:
  1. Inspect Context to identify the target and change needed.
  2. Use the signed-in/scoped operator path when a human-approved runtime or
     managed-secret mutation is required.
  3. Build a product-config request for `POST /v1/product-config/apply`.
  4. **Dry-run** and inspect redacted results.
  5. **Apply** with a concrete reason only after the dry-run succeeds and the
     operator intent is explicit.
  6. Inspect returned `next_actions` and complete required follow-up actions;
     product-config apply can update Launchplane records before the live target
     runtime has been synced.

Agents may guide the operator, prepare request shape, summarize redacted dry-run
evidence, and report trace IDs/status. Agents must not collect plaintext secret
values in chat, issues, PRs, docs, logs, or helper output, and must not bypass
Launchplane by editing provider configuration directly.

## Merge Train (Controller)

Use Launchplane's controller route as the default merge-train workflow.

- **Preferred Route**: `POST /v1/work-graph/merge-train/controller/run-once`.
- **Operator Action**: Put `ready-to-merge` only on the root PR that targets the
  protected base branch. Do not hand-collapse stacks in GitHub.
- **Controller Semantics**: Each call advances one safe phase at a time:
  same-repo linear stack-collapse planning/execution when needed,
  collapsed-root admission, candidate plan/build/observe, landing-plan
  creation, PR-native landing, and child PR disposition.
- **Retry Model**: Repeated controller calls are expected. Stop and report
  blocked, stale, denied, or failed states with compact evidence and trace IDs.
- **Troubleshooting**: Treat phase-specific merge-train endpoints as detail or
  recovery surfaces. They are not the default skill workflow.
- **Boundaries**: Merge-train behavior is DB/policy-backed. Do not hardcode
  repositories, labels, tokens, protected branches, or local file config.

## Intentionality & Safety

This skill combines inspection and mutation. You must explicitly announce when
you are transitioning from **Inspecting Context** to **Executing Operator
Actions**. Never apply a mutation without a preceding dry-run and situational
verification.

## Tools

- `scripts/launchplane-context.py`: Structural state helper.
- `POST /v1/product-config/apply`: Primary product-config operator path for
  signed-in/scoped operators; dry-run before apply.
- `POST /v1/work-graph/merge-train/controller/run-once`: Preferred merge-train
  controller path; call repeatedly to advance one safe phase at a time.
- Launchplane host-only CLI helpers: Use only when you are explicitly on the
  Launchplane host via SSH or the repo provides a concrete command. Do not
  assume a global `launchplane` binary exists on ordinary workstations.
