---
name: infra-ops
description: Use for infrastructure operations across private docs, local automation, hosts, ingress, network, storage, media, and managed service APIs. Trigger for read-only inventory, health checks, guarded pilot writes, rollback/snapshot planning, operator helper routing, and production-impacting infra changes after docs/access paths are known.
metadata:
  short-description: Operate private infrastructure safely
resources:
  - path: references/private-context.example.md
    kind: reference
    description: Public-safe example of the private operations context contract and repo pointer conventions.
  - path: references/npmplus-context-schema.md
    kind: reference
    description: Public-safe schema for the generic NPMplus engine and private context provider boundary.
  - path: scripts/npmplus-ops.py
    kind: script
    description: Generic redacted NPMplus operations engine driven by private context.
  - path: scripts/private-context-check.py
    kind: script
    description: Checks for configured private infra context without printing private values.
commands:
  - name: infra-ops-private-context
    source: skill
    resource_path: scripts/private-context-check.py
    example_argv: ["uv", "run", "scripts/private-context-check.py"]
    purpose: Checks whether the private operations docs pointer is configured without printing its value.
  - name: infra-ops-npmplus-context-check
    source: skill
    resource_path: scripts/npmplus-ops.py
    example_argv: ["uv", "run", "scripts/npmplus-ops.py", "context-check"]
    purpose: Validates the private NPMplus context provider without printing private values.
  - name: infra-ops-npmplus-pilot-status
    source: skill
    resource_path: scripts/npmplus-ops.py
    example_argv: ["uv", "run", "scripts/npmplus-ops.py", "pilot-status"]
    purpose: Reads redacted NPMplus inventory and pilot target summaries through private context.
---

# Infra Ops

Use this skill to operate infrastructure through private docs, local helpers,
and provider or service APIs without putting private topology or credentials in
this public skill repository.

Use `docs-lookup` first only when the task is still about finding docs,
authority, or an access path. Once the task is to inspect live infra, run a
health check, plan a change, create a canary, mutate an API, or verify/rollback
an operational change, this skill owns the workflow.

## Core Rule

The public skill is generic. The private operations repo is the authority for
environment-specific docs, scripts, hostnames, topology, secrets, and service
adapter details.

Resolve that private repo from `$CODE_HOME/local-context.toml`, falling back to
`$CODEX_HOME/local-context.toml` and then `~/.code/local-context.toml`, using
the `[docs].local_infra` key. That is the local pointer for the private
operations repo. Do not hard-code any private repo name, compatibility alias,
branding, or absolute user path into committed public docs, issues, or PRs.

If the local context file or key is missing, report that private infra context
is not configured and keep the workflow read-only. Do not guess from provider
dashboards, browser sessions, shell history, or `.env` files. Do not copy
private path values, hostnames, tokens, customer/site names, or topology into
this repo.

## Safety Tiers

- **Read-only inventory**: list docs, inspect config, query status endpoints,
  collect health evidence, and summarize current state. Start here.
- **Reversible pilot write**: create canary routes, temporary records, test
  users, scoped firewall entries, or feature-specific dry-runs only after
  documenting rollback and verification.
- **Production-impacting mutation**: require explicit user approval, host or
  account identity checks, snapshot or backup gates when relevant, redacted
  payload review, and a rollback path before apply.

When the target is ambiguous, pause mutation and keep working read-only until
the authority, blast radius, and rollback owner are clear.

## Workflow

1. Identify the target service, requested outcome, and likely blast radius.
2. Resolve the private operations repo from the configured pointer and read its
   docs index or relevant service docs before probing live systems.
3. Prefer private repo helpers over direct API calls for fragile or repetitive
   operations. Read helper usage before running it.
4. For live checks, verify account, host, cluster, tenant, or site identity
   before interpreting results.
5. For writes, capture the tier, intended diff, rollback, validation checks,
   and approval state before apply.
6. Apply the smallest scoped change through the private helper or documented
   provider API path.
7. Validate the outcome from an independent signal when practical, then record
   the private-safe result in the owning issue, PR, or private ops docs.

## Adapter Guidance

Keep concrete adapter details in the private repo. This skill may route among
adapter families such as ingress, DNS, virtualization, mesh networking, media
services, monitoring, and managed product/runtime APIs, but it should not bake
one site, product, hostname, or secret layout into `SKILL.md`.

For DNS or Cloudflare work, resolve the private operations repo through
`[docs].local_infra`, read the private DNS/Cloudflare docs or helper usage, and
keep token values, zone identifiers, account details, and rollback specifics in
that private source. Public summaries should stick to generic record types,
intended diffs, safety tier, and redacted verification results.

For NPMplus or ingress work, use the public generic NPMplus engine only through
the private context provider contract in
`references/npmplus-context-schema.md`. Keep the automation user, canary route,
remote validation commands, rollback snapshot, and service-specific guardrails
in the private repo. Treat the public engine as reusable API/redaction/dry-run
machinery, not as a place for site-specific defaults.

NPMplus lifecycle writes require `npmplus.ops.v2` context that binds the
canonical service origin, authenticated principal, per-ref target fingerprint,
per-ref allowed action, and typed private readiness evidence. Legacy v1 context
remains read-only during provider migration.

For Launchplane-managed runtime state, use the `launchplane` skill once the
resource is known to be managed by Launchplane. `infra-ops` can coordinate the
broader infrastructure workflow and hand off bounded Launchplane mutations to
that skill.

## Public Safety

Public outputs may name environment variable names, generic adapter categories,
high-level safety tiers, and redacted outcomes. Public outputs must not include
secret values, concrete private hostnames, private IPs, customer/site topology,
provider payloads, or private repo inventories.

If a private discovery changes the way operations should be performed, update
the private operations repo or leave a private-safe follow-up in the owning
GitHub issue. Do not park recovery-critical infra state only in chat.

## Repo Naming

The private repo may still use a legacy name. Treat that as an implementation
detail hidden behind config. If the local and remote repos are renamed later,
keep migration notes in private config so this public skill does not churn.
