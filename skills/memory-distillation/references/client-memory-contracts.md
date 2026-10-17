# Client memory contracts

Read before discovering stores, applying an approved memory change, or verifying
retirement. The workflow supports Codex and Claude Code; their
configured homes, consumed layers, and update capabilities must be established
separately.

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
| Owning client and location | Agent client, configured home/root, canonical store path, configuration source |
| Status | Active, historical, duplicate alias, inaccessible, or unknown; say what is unproven |
| Consumed layers | Registry, summaries, raw-memory derivatives, rollout summaries, update notes, generated skills, or other layers that this client actually consumes |
| Mutation contract | Supported tool or documented note location, append-only/direct-edit restrictions, regeneration trigger if available |
| Verification contract | Application evidence, normal retrieval entrypoint, and ability to start an independent fresh session |

Resolve aliases only within scope; deduplicate by canonical identity (resolved
path, or filesystem identity where needed), report all known names, and apply a
change once. Do not follow an alias outside scope. Unknown ownership or denied
access is a coverage gap, not permission to probe further. Former Every Code and Codex Lab
stores are historical evidence; do not make them active client requirements.

Check all consumed layers relevant to the requested subject. An empty `MEMORY.md`
can coexist with raw derivatives or generated skills still in use. Conversely,
files that exist but are no longer consumed may be historical rather than active
memory. Keep existence, ownership, consumption, and authority separate.

## Codex background consolidation

Use this section when an approved note has not appeared in generated memory.
Match the installed version before relying on these implementation details.
The sources below are pinned to Codex CLI 0.163.0, upstream commit
`83086188ae0614bd882818ff1aa23f3194962012`.
If the installed version differs, recheck the linked files at its installed tag;
treat these names, numbers and schema details as unverified until confirmed.

### Trigger and eligibility

The [startup implementation](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/memories/write/src/start.rs)
starts an asynchronous task for an eligible root session: non-ephemeral,
memories feature enabled, not a subagent, and a memory state DB available.
It runs Phase 1 rollout extraction, then Phase 2 consolidation. This path is
triggered by session startup, not a clock-based schedule or a note-file watcher.
Passing time, writing a note, resuming a turn or rebooting alone does not prove
that another eligible startup happened.

The [global job claim](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/state/src/runtime/memories.rs#L1073-L1217)
skips consolidation during retry backoff, while a running lease is active, or
for **six hours after the latest successful finish**. Even a successful
no-workspace-change pass records success. Calculate the next eligible time
from the actual job record; an eligible startup after that time can attempt a
run, but expiry is not a scheduled execution or completion promise.

Other gates remain relevant. The
[rate-limit guard](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/memories/write/src/guard.rs)
checks both reported windows; the default remaining-capacity threshold is 25%.
Phase 1's default six-hour rollout-idle requirement concerns extraction inputs,
not the global cooldown. See the version's
[configuration defaults](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/config/src/types.rs).
Do not lower a threshold or edit job timestamps to make a pending request run.

### Observe a run without changing it

Resolve the active Codex home, memory version and SQLite home first; SQLite
storage can be configured separately. In this version, v1 jobs are in
`memories_1.sqlite`, and v2 jobs are in `memories_v2_1.sqlite`. These are separate
from `state_5.sqlite`. Both versions use the same job kind and key shown below,
through the shared [memory-store implementation](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/state/src/runtime/memories.rs).
Inspect each enabled version separately (both when dual-writing). A row in an
inactive version's database is historical, not progress for the active version.
Verify the schema against the
[memory migrations](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/state/memory_migrations/0001_memories.sql)
before querying an unfamiliar installation. Use an existing database opened
read-only, replacing the placeholder with the resolved database for that version:

```sh
sqlite3 -init /dev/null -readonly /absolute/path/to/MEMORY_DATABASE.sqlite <<'SQL'
SELECT status, started_at, finished_at, lease_until, retry_at, last_error,
       datetime(started_at, 'unixepoch') AS started_utc,
       datetime(finished_at, 'unixepoch') AS finished_utc
FROM jobs
WHERE kind = 'memory_consolidate_global' AND job_key = 'global';
SQL
```

Keep this a one-shot query. `-init /dev/null` skips the user's SQLite startup file.
Read-only opening can still need [WAL shared-memory files](https://www.sqlite.org/wal.html#read_only_databases)
or briefly hold a read lock. An open failure is a coverage gap, not a reason to
copy the database or change permissions. Do not use `immutable=1` on a live DB;
it disables normal locking and change detection.

The raw timestamp fields are Unix seconds; the formatted columns are UTC.
Interpret the row together:

- `running` with an unexpired lease means the job is claimed. Lease updates
  show heartbeat activity, not proof the generated content is correct.
- `retry_at` in the future means backoff. A recent `finished_at` with no
  `last_error` can impose the success cooldown even when status is `pending`:
  the claim code tests exactly those fields. Success writes `finished_at` and
  clears `last_error`; new extraction work can enqueue the row without clearing
  either field.
- For a particular note, record its write time and observe a successful run whose
  `started_at` is after that time. A run that merely finishes after the note may
  already have synced its inputs before the note existed. A later start is only
  candidate execution evidence: read the resulting content to determine whether
  the note applied, then verify fresh retrieval.

The [Phase 2 implementation](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/memories/write/src/phase2.rs)
syncs inputs, uses the workspace diff to decide whether to start a consolidation
agent, heartbeats its lease and records success or failure. The agent's
user-facing completion notifications are disabled. Logs/metrics may explain
skips and failures, but missing log lines or a missing `phase2_workspace_diff.md`
do not establish that no run occurred. A completed no-change pass also need not
rewrite the registry. Keep job execution, note application and retrieval as
separate observations.

The native [`memory/status` handler](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server/src/request_processors/memory_status.rs)
reports **v2 readiness only**: consolidated-thread count and summary validity
against a minimum count (20 by default). It is not an in-progress indicator,
does not report the last run, and cannot establish v1 consolidation status.
Read-only metadata inspection does not authorize DB edits, a memory reset or
manual changes to generated files; use the owning update contract below.

## Claude Code stores

Claude Code keeps memory per repository rather than per client home. Confirm
these points against the installed version's
[memory documentation](https://code.claude.com/docs/en/memory.md) and
[subagent documentation](https://code.claude.com/docs/en/sub-agents.md) before
relying on them:

- **Auto memory:** one store per git repository, shared by its worktrees, under
  `~/.claude/projects/<project>/memory/` unless an `autoMemoryDirectory` setting
  moves it. The `autoMemoryEnabled` setting or the
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY` environment variable can turn it off; record
  which settings file or variable applies.
- **Consumed layers:** each session loads the `MEMORY.md` index up to its line
  and size limit; topic files load on demand. Index entries past the limit are
  not loaded at start, but their topic files can still be read.
- **Subagent memory:** a subagent's `memory` field keeps a separate store per
  scope. `user` and `local` scopes are outside version control; `project` scope
  lives in the repository at `.claude/agent-memory/<name>/` and is committed by
  default, so change it through the repository's normal branch and PR workflow.
- **Scratch stores:** project folders are created for every directory a session
  runs in, including evaluation workspaces and temporary checkouts. Report them
  separately; an empty or orphaned folder is not an active store. A worktree's
  folder has an empty `memory/` because the repository's store serves it; that
  is expected, not lost memory.
- **Mutation contract:** memory files are plain Markdown the model edits directly,
  with no regeneration step. An approved change edits or removes the topic file
  and updates its `MEMORY.md` entry in the same action. Applied evidence is the
  resulting index and topic files, inspected after the edit.
- **Fresh-session verification:** start a new session from a directory in the
  same repository with auto memory enabled, for example a non-interactive
  `claude -p` run, and observe the index or topic-file retrieval in its
  transcript. The session saves memories during ordinary work, so deny it file
  writes by limiting its tools, for example
  `claude -p --tools Read,Grep,Glob --strict-mcp-config`. `--tools` limits only
  the built-in tools, `--strict-mcp-config` drops configured MCP servers, and
  pre-approving tools with `--allowedTools` leaves the user's own write
  permissions in place. If writes cannot be denied, report verification as
  pending. For a subagent store, invoke that subagent in the fresh session,
  since the main conversation does not load its memory.

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

- **Multiple stores:** Codex uses a configured home and Claude Code uses a different one.
  A second name resolves to the Claude Code store. Audit both if authorized, report the
  alias once, and do not copy their contents into a common memory directory.
- **Narrow scope:** The request names only Claude Code. A Codex location is known from
  configuration, but its contents remain out of scope.
- **Delayed consolidation:** An approved note retires an obsolete deployment
  claim; the registry still recommends it. Report `recorded, pending
  application`; a fresh session repeating it cannot be called verified.
- **Empty registry:** A registry is empty but a consumed raw-memory derivative
  retains an old PR status. Verify current project state and propose retirement
  of that transient claim without removing a durable useful preference beside it.
- **Memory restating a skill:** a Claude Code project memory records a helper
  command that a catalog skill owns. Confirm the skill carries the command from
  the runtime checkout, then propose removing the topic file and its index entry.
  A fresh session in that repository should load the skill rather than recall
  the command.
