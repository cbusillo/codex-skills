---
name: partdb
description: Use when the user asks to find, organize, count, categorize, locate, or safely update components, tools, consumables, or other inventory in a Part-DB instance. Trigger for Part-DB inventory workflows, API or MCP integration choices, schema discovery, read-only stock lookup, intake planning, and proposed inventory changes. Do not use for a Part-DB container, proxy, backup, migration, or host operation; use `infra-ops` instead.
metadata:
  short-description: Manage Part-DB inventory safely
resources:
  - path: references/private-context.md
    kind: reference
    description: Public-safe contract for resolving a private Part-DB instance and local inventory conventions.
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
expected fields, and output rules.

If no valid private context is available, explain what is missing without
guessing an endpoint, searching shell history, inspecting unrelated `.env`
files, or inventing inventory conventions.

## Target Safety Tiers

The tiers below define the contract for future integrations. This version does
not access an API or execute a tool call.

1. **Read-only:** Search, inspect schema, find locations, and verify stock. A
   future helper must use a scoped read credential after local contract checks.
2. **Propose:** Turn supplied evidence into a structured draft. Show unknown or
   ambiguous fields explicitly; never fabricate part numbers, quantities,
   locations, or categories.
3. **Mutate:** Create, update, move, adjust, or delete inventory only after the
   user explicitly approves the rendered change set. Re-verify the target and
   result, and do not reuse approval for later changes.

Do not improvise request payloads from general memory. A later workflow must
discover the instance's OpenAPI contract before issuing even read-only API
calls.

## Workflow

1. Classify the request as inventory planning, read-only lookup, a proposed
   change, or a Part-DB service operation.
2. Route service operations to `infra-ops`. For inventory work, resolve private
   context without exposing it in public artifacts.
3. For proposals, gather the operator's evidence and produce a reviewable
   draft. Keep item type, category, physical location, quantity, unit, and
   source evidence separate.
4. For a future read-only integration, validate the instance's schema and
   identity before querying it. For a future mutation, render the complete diff
   and wait for explicit approval.
5. Keep durable local taxonomy and location decisions in the private source of
   truth; update public skill guidance only when the generic contract changes.

## Public Safety

Do not commit or copy instance URLs, credentials, supplier accounts, inventory
exports, part names, serial numbers, quantities, storage maps, photos, or
attachments into public skill files, issues, pull requests, examples, or logs.

When reporting to the user in a private session, minimize sensitive inventory
data to the requested task. Redact configuration values and do not expose
tokens, authorization headers, or private network details.

## Future Work

The follow-on read-only workflow owns API-schema discovery and deterministic
lookup. The separate mutation workflow owns preview, explicit approval,
idempotency, and post-write verification.
