---
name: launchplane-operator
description: Use when a task needs Launchplane-owned runtime env, managed secrets, product config, live target runtime sync, deploy or promotion orchestration, or operator mutations from any repo. Prefer Launchplane service or CLI paths over provider-direct mutation.
---

# Launchplane Operator

Use this skill whenever an agent needs to inspect or mutate Launchplane-owned
product, runtime, secret, deployment, promotion, or live-target state from any
repo.

## Credential Location

Local operator credentials live outside repos:

```text
~/.config/launchplane/local-operator.env
```

Expected keys:

```text
LAUNCHPLANE_LOCAL_OPERATOR_TOKEN
LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT
LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL
```

Treat that file as a local secret. Do not commit it, paste token values, or copy
secret values into chat, issues, PRs, docs, or logs.

## Operating Rules

- Do not mutate Dokploy, provider env, or product-host config directly.
- Use the deployed Launchplane service API or Launchplane CLI paths backed by
  the service boundary.
- Dry-run before apply.
- Apply requires a non-empty reason.
- Secret-backed writes must remain redacted in responses and logs.
- Runtime key-safety failures are blockers, not warnings.
- After product-config apply, run live-target-runtime sync/deploy when the
  running target needs Launchplane-owned values applied to Dokploy.
- Report key names, actions, counts, trace IDs, and status only.

## Product Config Flow

1. Identify product, context, and instance.
2. Read required local product values only when needed.
3. Build a Launchplane product-config request with runtime keys and managed
   secret binding keys.
4. Dry-run through Launchplane and inspect redacted evidence.
5. Apply through Launchplane with a reason if dry-run passes.
6. Run the sanctioned live-target-runtime sync/deploy path if the live target
   must receive the updated runtime values.

## When Blocked

If local operator credentials are missing or rejected, stop and report that the
Launchplane local operator credential is unavailable. Do not fall back to direct
provider mutation.
