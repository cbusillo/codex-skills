---
name: memory-distillation
disable-model-invocation: true
description: Use only when the user explicitly asks to audit, clean, prune, archive, reset, or distill Codex or Codex Lab memories into skills, repo docs/issues, or local config. Never use implicitly or for ordinary repo work.
metadata:
  short-description: Audit memories into durable sources
policy:
  allow_implicit_invocation: false
---

# Memory Distillation

Audit memories for Codex and Codex Lab, preserve useful knowledge in maintained
sources, and verify approved retirement from the context each client reuses.
Use this workflow only for an explicit memory audit or change request. A completed
audit produces a supported proposal; a completed retirement requires evidence
from the affected stores and a fresh session's normal retrieval path.

Apply [task scope and authorization](../references/execution-scope.md). Install
this skill with the catalog's shared `../references/` directory. Read
[client memory contracts](references/client-memory-contracts.md) before discovery,
updates, or retirement verification. When changing this skill, use the
[synthetic evaluation cases](evaluations/README.md); that is not a request to
inspect the user's memories.

## Scope and approval

- Do not activate implicitly or for ordinary repo work.
- Start with a read-only inventory, audit, and concrete proposal. Do not edit,
  delete, move, archive, or generate memory files, skills, repo docs, issues, or
  local config until the user approves that action and scope. Reuse approval
  already given for the same action and scope; ask only for missing authority.
- Honor a request limited to one client, store, host, or subject. Discovering
  another store does not authorize reading or changing it. Discovery stays
  within authorized configuration and documented locations, without disk sweeps
  or cross-host access.
- Use the owning client's supported update mechanism. An additive-note contract
  permits an approved note, not direct edits to generated memory, resets, or
  copying/symlinking stores together. A reset is a separate destructive action.
- Preserve original sessions, rollout records, and historical archives. Retiring
  their influence on reusable context does not authorize deleting the originals.
- Never put secrets, credentials, private hostnames, customer/client data, private
  messages, local-only paths, machine-specific values, or personal account
  details into public skills or reports.

## Source roles

Memories are candidate observations. Current maintained sources and verified
state take precedence: code and helper contracts for behavior, repo docs and
current GitHub state for project work, and local config for private values.
Neither client nor a directory name makes a memory store authoritative.

- **Skills:** reusable, durable, public-safe procedures and workflow preferences.
- **Repo docs / GitHub issues:** repo-specific design, plans, and follow-up work.
- **Local config:** private or environment-specific facts with a maintained
  schema. Use the optional `people` skill and private people config for durable
  identity, aliases, contacts, roles, and relationship context.
- **Historical evidence:** useful provenance, clearly non-authoritative. Every
  Code is retired as a supported client; classify any discovered former store
  from current evidence rather than treating its path as proof of ownership.
- **Legacy Chronicle archives:** private historical screen-history data. This
  repository does not provide or require an active Chronicle skill. Treat every
  legacy Chronicle archive as historical evidence. Read the
  [historical archive procedure](references/historical-archives.md) only when the
  user explicitly requests Chronicle or screen-history distillation.

## Workflow

1. Resolve the requested scope and inventory each relevant configured store,
   including non-default client homes. Record client ownership, canonical path,
   aliases, active/historical/unknown status, consumed layers, supported update
   mechanism, and verification capability. Report inaccessible or out-of-scope
   locations without reading past that boundary. An empty registry does not
   establish that the store or its derivatives are empty.
2. Search the authorized layers for stale behavior, transient status, private
   details, duplicate rules, and unique useful facts. Prefer targeted retrieval
   and recent summaries; broaden only when needed to cover the requested audit.
3. Classify candidates as `promote-to-skill`, `promote-to-repo-doc-or-issue`,
   `move-to-people-local-config`, `move-to-local-config`, `keep-historical`, or
   `retire-from-reusable-context`. Propose deletion/archive only when requested
   and supported; preserve historical originals by default.
4. Verify each recommendation against maintained sources. Do not turn an old
   open PR, active job, or CI snapshot into a permanent fact. Before retiring a
   unique useful rule, verify its replacement exists and is reachable by the
   target client from the intended checkout; a future plan or an uninstalled
   worktree is insufficient.
5. Present affected entries, destination or retirement action, reason, evidence,
   and any authority still needed. Preserve stable useful facts. When private
   audit artifacts contain person mentions, use the optional `people` artifact
   review to route durable facts to approved private config.
6. Carry approved actions through each store's supported mechanism, once per
   canonical store. If a client only accepts notes, record the scoped note and
   preserve generated files. If regeneration is unavailable, report the pending
   capability and continue independent authorized work; do not invent a command
   or seek approval again for the same action.
7. Check application and then fresh-session retrieval as separate stages, using
   the evidence rules in the client reference. If stale guidance still surfaces,
   keep retirement pending. Report only the stage actually demonstrated for
   each candidate and store.

## Result

Return a concise account of audited scope and coverage gaps, proposed or approved
actions, maintained destinations, and evidence. Distinguish **requested**,
**recorded**, **applied/regenerated**, and **retrieval-verified** per affected
store. State what remains pending and the next supported action. A saved note,
changed hash, or clean report alone does not prove retirement from reusable
context.
