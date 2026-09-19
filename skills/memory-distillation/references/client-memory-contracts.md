# Client memory contracts

Read before discovering stores, applying an approved memory change, or verifying
retirement. The workflow supports Codex and Codex Lab; their configured homes,
consumed layers, and update capabilities must be established separately.

## Discover within scope

Use the current session instructions, authorized client configuration, installed
client help, or source matching the installed version. Do not assume a fixed pair
of homes, one client's environment-variable precedence, or that the active
session's store is the only configured store. Record the source of each mapping.
Use documented defaults only for clients with evidence of installation and only
within the user's scope. Do not enumerate the whole filesystem or inspect
unrelated client profiles to make an inventory look complete.

Inventory each distinct in-scope store:

| Field | Evidence to record |
| --- | --- |
| Owner and location | Client, configured home/root, canonical store path, configuration source |
| Status | Active, historical, duplicate alias, inaccessible, or unknown; say what is unproven |
| Consumed layers | Registry, summaries, raw-memory derivatives, rollout summaries, update notes, generated skills, or other layers that this client actually consumes |
| Mutation contract | Supported tool or documented note location, append-only/direct-edit restrictions, regeneration trigger if available |
| Verification contract | Application evidence, normal retrieval entrypoint, and ability to start an independent fresh session |

Resolve aliases only within scope; deduplicate by canonical identity (resolved
path, or filesystem identity where needed), report all known names, and apply a
change once. Do not follow an alias outside scope. Unknown ownership or denied
access is a coverage gap, not permission to probe further. A former Every Code
store can be historical evidence; do not make it an active client requirement.

Check all consumed layers relevant to the requested subject. An empty `MEMORY.md`
can coexist with raw derivatives or generated skills still in use. Conversely,
files that exist but are no longer consumed may be historical rather than active
memory. Keep existence, ownership, consumption, and authority separate.

## Apply the owning client's contract

Before writing, establish the supported mechanism from current instructions and
installed capabilities. Do not derive a write contract from a neighboring
client, an old rollout, or a familiar path. If the contract accepts only additive
notes, create the approved note there, with a narrow instruction to update or
retire the identified claim while retaining useful facts and original evidence.
Do not edit generated registries, summaries, or skills directly, remove old
notes, or synchronize independent stores to bypass that contract.
Keep retirement notes concise: request removal of the obsolete claim, rather
than leaving a growing corrective narrative in reusable context. Preserve
unresolved work and stable useful facts that the retirement does not supersede.

A supported regeneration operation can be used within existing authorization
for applying the approved change. Its availability is a capability question,
not a reason to ask repeatedly for the same permission. If the client performs
consolidation asynchronously or exposes no supported trigger, stop at the
observable stage and name the pending operation. A reset is not selective
retirement. Do not improvise database edits or a new memory engine.

For promotion, verify the maintained destination's content, ownership, and
availability to the target client. Preserve the useful source claim until that
replacement is available. A rule committed only in a task worktree may not yet
be accessible from the runtime checkout.

## Evidence for each stage

Track a candidate separately in each affected canonical store. Partial success
in one store does not establish completion for another.

| Stage | Minimum evidence |
| --- | --- |
| Requested | User authorization identifying the action and scope; record what still needs approval |
| Recorded | The supported note/tool receipt and inspected request content identify the intended change; this does not show consolidation happened |
| Applied/regenerated | The owning client's successful application evidence and inspected resulting content show the relevant consumed derivatives updated; record unchanged layers and why |
| Retrieval-verified | An independent fresh session uses the client's normal retrieval path and demonstrates the stale claim no longer governs the answer while a preserved useful fact is still retrievable |

Hashes help bind evidence to a source revision; they do not establish semantic
correctness. Inspect the affected content after application, including any
consumed derivatives that could reintroduce the retired rule. Preserve original
rollouts and archives even when their generated summaries require retirement.

For fresh-session verification:

1. Use a neutral task that would previously have retrieved the stale claim. Do
   not supply the desired correction, expected answer, or prior audit transcript.
2. Include a useful retained fact as a retrieval control. Absence alone can mean
   retrieval is broken or disabled. Verify actual retrieval provenance in the
   new session; a claim of having checked memory is insufficient.
3. Distinguish a historical quotation identified as obsolete from guidance still
   presented as current. If stale guidance remains authoritative, or a consumed
   layer remains unchecked, mark the candidate pending.
4. Record client/version, resolved store, relevant source revision, observed
   retrieval, result, and limits. If a supported fresh session or retrieval
   observation is unavailable, say `applied, not retrieval-verified` (or the
   earlier stage actually reached).

## Small examples

- **Multiple stores:** Codex uses a configured home and Lab uses a different one.
  A second name resolves to the Lab store. Audit both if authorized, report the
  alias once, and do not copy their contents into a common memory directory.
- **Narrow scope:** The request names only Lab. A Codex location is known from
  configuration, but its contents remain out of scope.
- **Delayed consolidation:** An approved note retires an obsolete deployment
  claim; the registry still recommends it. Report `recorded, pending
  application`; a fresh session repeating it cannot be called verified.
- **Empty registry:** A registry is empty but a consumed raw-memory derivative
  retains an old PR status. Verify current project state and propose retirement
  of that transient claim without removing a durable useful preference beside it.
