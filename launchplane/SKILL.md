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
- **Helper Contract**: Use `references/write-action-helper-contract.md` for
  bounded helper entrypoints, exit behavior, and redacted output shape.
- **Auth**: Prefer signed-in, scoped operator sessions in the Launchplane UI or
  service API. Source terminal/local operator credentials only through the
  operator contract; do not paste token values into chat, issues, PRs, docs, or
  logs.
- **Private Config**: For non-browser terminal execution, use the source order
  in the operator contract. Missing private config means the write-capable path
  is unavailable and must fail closed; do not use `.github/github.override.json`
  for Launchplane credentials.
- **First Shot**: For product-config/runtime/secret sync, use the service API
  path from the operator contract first. Do not start by searching for a local
  `launchplane` binary or by poking provider config directly.
- **Workflow**:
  1. Inspect Context to identify the target and change needed.
  2. Preflight product-config intent with `scripts/launchplane-write-action.py
     product-config-preflight` when agent-side authorization or managed-secret
     binding evidence is useful.
  3. Use the signed-in/scoped operator path when a human-approved runtime or
     managed-secret mutation is required.
  4. Build a product-config request for `POST /v1/product-config/apply` only in
     an approved operator surface. The helper may submit dry-run/apply from a
     private local payload file, never from chat, CLI plaintext secret args, or
     committed examples.
  5. **Dry-run** and inspect redacted results.
  6. **Apply** with a concrete reason only after the dry-run succeeds and the
     operator intent is explicit.
  7. Inspect returned `next_actions` and complete required follow-up actions;
     product-config apply can update Launchplane records before the live target
     runtime has been synced.

Agents may guide the operator, prepare request shape, summarize redacted dry-run
evidence, and report trace IDs/status. Agents must not collect plaintext secret
values in chat, issues, PRs, docs, logs, or helper output, and must not bypass
Launchplane by editing provider configuration directly.

## Merge Train (Controller)

Use Launchplane's controller route as the default merge-train workflow.

- **Preferred Route**: `POST /v1/work-graph/merge-train/controller/run-once`.
- **Helper**: Use `scripts/launchplane-write-action.py
  merge-train-controller-run-once` instead of open-coding the route. Mutating
  calls require an idempotency key.
- **Operator Action**: Put `ready-to-merge` only on the root PR that targets the
  protected base branch. Do not hand-collapse stacks in GitHub.
- **Controller Semantics**: Each call advances one safe phase at a time:
  same-repo linear stack-collapse planning/execution when needed,
  collapsed-root admission, candidate plan/build/observe, landing-plan
  creation, PR-native landing, and child PR disposition.
- **Proven Batch Flow**: The controller has been proven against a live
  multi-PR batch train. It can reflow a failed candidate when the eligible queue
  changes, build and observe a replacement candidate, create a landing plan,
  land the original PRs through GitHub's PR merge API in train order, and post
  managed feedback to each PR. Treat this as the normal rollout path, not an
  experimental one-off.
- **Mutation Gate**: Keep scheduled runners in dry-run mode until the operator
  explicitly selects a mutation pilot. Manual `mutate=true` controller runs are
  appropriate only after dry-run evidence shows the intended queue, candidate,
  and next action. Do not leave scheduled mutation enabled as a casual default.
- **Stacked PRs**: For a same-repo linear stack, label only the root PR that
  targets the protected base branch. Let Launchplane collapse child branches
  into that root, wait for the root head SHA to satisfy checks, admit only the
  root to the flat train, and resolve child PRs after the root lands according
  to policy. Treat forked, ambiguous, sibling, cyclic, stale-head, or
  permission-limited stacks as blocked/unsupported instead of mutating by hand.
- **Retry Model**: Repeated controller calls are expected. Stop and report
  blocked, stale, denied, or failed states with compact evidence and trace IDs.
- **Evidence**: For stack runs, report the stack-collapse plan record id, any
  batch candidate record id, the landing-plan record id, workflow run URLs, and
  the final root merge commit. Include child disposition evidence when the root
  lands.
- **Batch Evidence**: For flat batch runs, report the dry-run/admission reason,
  candidate record id and candidate SHA, required-check status on the candidate
  commit, landing-plan record id, each landed PR number and merge commit, managed
  feedback delivery status, and post-merge checks on the target repository's
  default branch.
- **Recovery Evidence**: If Launchplane patches are needed during rollout,
  verify their PR checks, post-merge CI/Security/CodeQL, and Deploy Launchplane
  before retrying mutation. Record the failing workflow run id and trace id that
  motivated the patch.
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
- `scripts/launchplane-write-action.py`: Public-safe write-action wrapper for
  product-config intent preflight, private local product-config dry-run/apply,
  and merge-train controller calls.
- `POST /v1/agent/write-intents/evaluate`: Product-config preflight surface for
  authorization and managed-secret binding evidence; never carries plaintext.
- `POST /v1/product-config/apply`: Primary product-config operator path for
  signed-in/scoped operators; dry-run before apply.
- `POST /v1/work-graph/merge-train/controller/run-once`: Preferred merge-train
  controller path; call repeatedly to advance one safe phase at a time.
- Launchplane host-only CLI helpers: Use only when you are explicitly on the
  Launchplane host via SSH or the repo provides a concrete command. Do not
  assume a global `launchplane` binary exists on ordinary workstations.
