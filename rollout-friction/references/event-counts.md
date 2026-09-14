# Event counts and report versions

Read this reference when interpreting analyzer or episode counts, comparing
reports, or giving reports and trajectory skeletons to a model.

For long inputs, the analyzer and segmenter accept `--progress` for redacted
file/stage updates on stderr and `--max-seconds` for a cooperative analysis
budget. Its clock starts after file discovery and target planning; final report
encoding and output are outside that clock. Budget checks occur between records,
fragments, and stages (between files for episode grouping); they cannot
interrupt a single slow I/O, parser, or regex operation. Use an outer process
deadline when a hard limit is required. On budget exhaustion the command exits
2 and discards the entire partial report. JSON mode emits `ok: false` with a
`scan_time_limit` limitation and no findings or episode arrays; text/JSONL mode
keeps stdout empty and reports the failure on stderr. The clusterer rejects a
failed JSON report. Check the command exit status before consuming JSONL: an
empty output from a failed scan is not a successful scan with zero episodes.

Snippet limits apply after redaction. Matching uses the full bounded input;
performance optimizations do not truncate signal evidence or change version-2
counts. A time-limited run cannot establish complete coverage or absence of
friction.

New reports declare `schema_version: 2` and
`count_semantics: normalized_events_v2`. The analyzer's
`repeated_command_failure.count` counts distinct failed result events. Other
signals retain their text-match counts, identified by `count_unit`; their counts
are triage evidence rather than execution totals.

## Evidence and identity

The analyzer and segmenter use the same normalization. A tool result's typed
exit status or error flag takes precedence over words in its output. Arguments,
typed user/assistant messages, and session instructions do not supply executed
outcomes. Once an outer result has a terminal status, serialized JSON printed in
its output or stdout remains data. Explicit result batches retain separate child
outcomes; multiple fields, diagnostic phrases, or summary fragments do not
multiply one result.

Typed textual tool results such as `function_call_output` containing exactly
`exit_code=1` carry `outcome_basis: result_text`; the same text in a user message
or untyped copied snippet does not gain that provenance. A recognized leading
terminal header is read before filtering investigation chatter. Printed `Output`
content cannot override that header, including a successful zero exit.
That boundary persists across content fragments; a body without a preceding
terminal header cannot supply an outcome through fallback text matching.
Native Codex exec JSONL accepts dotted `item.started`/`item.updated`/
`item.completed` phases and the equivalent `item_started`/`item_updated`/
`item_completed` spellings, with either `command_execution` or
`CommandExecution` as the command item type. These forms may appear directly
or inside `event_msg`/`response_item` payload envelopes, including nested ones.
These forms share `item.id` as their invocation identity and use the terminal
command status. Starts and updates remain non-terminal until completion, even
if they contain error prose or terminal-looking fields. Native items carrying
a user, assistant, system, or developer role remain context at every envelope
level and cannot establish later command correlation.
Other native item kinds, including `FileChange`, agent messages, reasoning, and
model metadata warnings, remain context. Their completed status and embedded
source text do not establish a command outcome.
Recognized lifecycle envelopes without a valid item remain context; fields on
an incomplete envelope do not establish a command result.

Mirrors with the same call identity in one source session count once. Separate
calls and identified sessions remain distinct. Without a call identity, a result
uses its record and batch position; ambiguous idless mirrors cannot safely be
deduplicated by matching their text. Standalone structured result maps and legacy
text logs remain supported. Untyped text outcomes carry `outcome_basis: text_hint`
and cannot establish tool provenance. Malformed or truncated structured records
do not fall back to executing-result interpretations of their text.

An explicit unfinished result, such as a null exit status or a numeric live exec
`session_id`, does not establish a terminal failure. An opaque session UUID alone
does not indicate an unfinished command. Earlier invocation context within the
bounded input can associate a result with its command even when a timestamp or
line checkpoint excludes that invocation from reported counts.

Scanner read errors survive timestamp and line filters as redacted
`scan_limitations`, without becoming friction events. The segmenter's JSONL mode
reports read diagnostics on stderr so stdout remains an episode-only stream.

## Episode costs

| Field | Version 2 meaning |
| --- | --- |
| `event_count` | Distinct normalized records/results contributing a reported signal. Multiple signals on one event count once. |
| `tool_call_count` | Distinct invocation identities in the episode window. A matched call and result count once; orphan result identities stand in for calls absent from the bounded input. |
| `retry_count` | Repeated known commands following their failed result. A call and its result share one retry identity. Prose intentions and retry-related words do not count. |
| `failure_count` | Failed results and legacy failure hints, excluding narrowly recognized expected search statuses. This does not prove the failure was unexpected. |
| `nonzero_exit_count` | All available nonzero exit statuses, including recognized expected ones. |
| `expected_nonzero_count` | Exit 1 from a simple standard `rg` or `grep` invocation without an explicit tool error. Compound shell commands, pipelines, redirects, substitutions, and arbitrary wrappers do not qualify. |
| `text_hint_failure_count` | The subset of failures inferred from untyped text rather than a structured result status. |

The analyzer and JSON episode report also expose `outcome_summary`, including
raw nonzero counts and bounded examples of expected nonzero statuses. Expected
statuses retain their exit code and redacted source identity even when no
friction episode meets a threshold.

The classification assumes normal `rg`/`grep` exit semantics; it does not infer
whether another tool's nonzero exit was intentional. Missing invocation context
prevents that exception and may prevent retry identification. Episode boundaries,
outcome labels, and cost weights remain proximity-based triage heuristics. They
do not establish task resolution, unexpected friction, elapsed time, prevalence,
or a performance improvement.

## Migration and downstream use

Version 1 used fragment and keyword counts. Clustering still accepts a report
containing only legacy episodes and labels its clusters and skeletons
`legacy_fragment_hits_v1`. It rejects mixed legacy/version-2 episodes, mismatched
envelope/record versions, and unknown count meanings with migration guidance.
Regenerate legacy episodes from the same authorized source set or cluster each
version separately. Changing only a version label does not convert old counts.

New episode and cluster identities include the new interpretation. Keep the
count semantics and outcome provenance when summarizing or reviewing output,
including model scout input. Compare costs only within one interpretation and
matched source scope; lower counts after this correction do not demonstrate
less workflow friction or faster execution.
