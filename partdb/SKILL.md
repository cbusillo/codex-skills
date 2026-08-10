---
name: partdb
description: Use when the user asks to find, organize, count, categorize, locate, or safely update components, tools, consumables, or other inventory in a Part-DB instance. Trigger for Part-DB inventory workflows, API or MCP integration choices, schema discovery, read-only stock lookup, intake planning, and proposed inventory changes. Do not use for a Part-DB container, proxy, backup, migration, or host operation; use `infra-ops` instead.
metadata:
  short-description: Manage Part-DB inventory safely
resources:
  - path: references/private-context.md
    kind: reference
    description: Public-safe contract for resolving a private Part-DB instance and local inventory conventions.
  - path: scripts/partdb-read.py
    kind: script
    description: Read-only Part-DB schema, search, and location helper driven by private context.
  - path: scripts/partdb-write.py
    kind: script
    description: Preview-gated, read-back-verified part-lot quantity update helper driven by private context.
commands:
  - name: partdb-context-check
    source: skill
    resource_path: scripts/partdb-read.py
    example_argv: ["uv", "run", "scripts/partdb-read.py", "context-check"]
    purpose: Validates the private read-only context without printing values.
  - name: partdb-write-plan
    source: skill
    resource_path: scripts/partdb-write.py
    example_argv: ["uv", "run", "scripts/partdb-write.py", "plan", "--intent", "private/intent.json", "--output", "private/plan.json"]
    purpose: Produces a reviewable, signed plan for one part-lot quantity update without mutation.
---

# Part-DB Inventory

Use this skill for Part-DB inventory workflows. Keep this public skill generic;
the instance, credentials, inventory, and local organization conventions belong
in private context.

## Scope

Use Part-DB for countable items with a physical location or stock-management
reason, such as components, tools, hardware, consumables, and supplies. Do not
force project plans, maintenance history, warranties, receipts, or unrelated
one-off possessions into an inventory workflow.

Do not configure an MCP server as a side effect. If the user already has a
trusted Part-DB MCP server, treat it only as a transport and keep the same
private-context and approval boundaries below.

## Private Context

Before accessing an instance, resolve the configured private context. Read
`references/private-context.md` for the context contract, resolution order,
expected fields, first-configured-pointer fallback behavior, and output rules.

If no valid private context is available, explain what is missing without
guessing an endpoint, searching shell history, inspecting unrelated `.env`
files, or inventing inventory conventions.

## Target Safety Tiers

The tiers below define the contract. The bundled helpers implement the
read-only tier and a deliberately narrow mutation path for part-lot quantity
updates.

1. **Read-only:** Search, inspect schema, find locations, and verify stock. A
   helper uses a scoped read credential after local contract checks.
2. **Propose:** Turn supplied evidence into a structured draft. Show unknown or
   ambiguous fields explicitly; never fabricate part numbers, quantities,
   locations, or categories.
3. **Mutate:** Create, update, move, adjust, or delete inventory only after the
   user explicitly approves the rendered change set. Re-verify the target and
   result, and do not reuse approval for later changes.

Do not improvise request payloads from general memory. Probe the installed
instance's OpenAPI contract before relying on version-sensitive endpoints.

## Workflow

1. Classify the request as inventory planning, read-only lookup, a proposed
   change, or a Part-DB service operation.
2. Route service operations to `infra-ops`. For inventory work, resolve private
   context without exposing it in public artifacts.
3. For proposals, gather the operator's evidence and produce a reviewable
   draft. Keep item type, category, physical location, quantity, unit, and
   source evidence separate.
4. Run `partdb-context-check`, then a schema probe before querying inventory.
   For a mutation, generate a plan from a private intent file, render its exact
   diff, and wait for explicit approval of that plan digest.
5. Keep durable local taxonomy and location decisions in the private source of
   truth; update public skill guidance only when the generic contract changes.

## Quantity Updates

The bundled mutation helper supports only setting a single existing part lot's
quantity. It does not create, delete, move, or attach records. Keep its intent,
plan, approval, and receipt files in a private ignored directory.

1. Create an intent with only `op`, `lot_id`, and `amount`; `op` must be
   `part-lot-amount-set`.
2. Run `partdb-write-plan`. It validates the installed OpenAPI schema, reads
   the current amount, and emits a plan with an exact digest.
3. Show the plan's `prior_amount` and `target_amount` to the user. Do not run
   the next step until the user explicitly approves that exact digest.
4. Run `partdb-write.py approve --plan ... --approve <digest> --output ...`.
5. Run `partdb-write.py apply --plan ... --approval ... --apply`.
   It reads again, refuses drift, patches only after those checks, reads back to
   verify, and records a redacted digest-keyed receipt beside the plan. Each approval is single-use;
   a failed apply leaves a `needs-reconciliation` receipt instead of retrying a
   possibly completed write.

The private context must declare a separate write-token environment variable
and `allow_mutations = true`; that only enables this helper's exact-plan gates,
not unreviewed mutations.

## Public Safety

Do not commit or copy instance URLs, credentials, supplier accounts, inventory
exports, part names, serial numbers, quantities, storage maps, photos, or
attachments into public skill files, issues, pull requests, examples, or logs.

When reporting to the user in a private session, minimize sensitive inventory
data to the requested task. Redact configuration values and do not expose
tokens, authorization headers, or private network details.

## Future Work

Part and attachment creation, location moves, and deletion require their own
schema-driven, idempotent proposal workflows. The read-only helper also
provides `context-check`, `schema-probe`, `search`, and `locations` commands.
