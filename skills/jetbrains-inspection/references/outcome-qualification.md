# Outcome Qualification and Diagnostics

Read before running `summarize-outcomes --qualification-file`, diagnosing
semantic coverage or attribution, configuring outcome logs, or changing these
helper proof contracts.

## Attribution and Outcome Logs

Every `GREEN`, `RED`, or `UNKNOWN` result and cleanup anomaly carries
`inspection_attribution` schema version 1 with a stable classification, code,
phase, endpoint, HTTP status, helper/plugin provenance, cleanup state, and
bounded evidence IDs. Preserve plugin attribution and prefer its IDE channel
over selector fallback. The helper supplies one `client_run_id` per invocation
and preserves plugin `request_id` values. `unattributed_unknown: true` is a
helper/tool failure, not a neutral unknown bucket.
Dirty plugin fingerprints remain provenance rather than presumed causation.
Qualification must use the exact intended fingerprint, but normal diagnostics
should report `plugin_build_dirty` without claiming it caused the verdict.
A concurrent `inspection_in_progress` response is adoptable only when it
includes an unambiguous positive run ID plus explicit scope/profile proof that
exactly matches the trigger request. For `changed_files`, it must also prove
the same unversioned-file policy and changed-files mode. Legacy, missing,
partial, mismatched, or contradictory conflict payloads fail closed as
`inspection_proof_failed` at the trigger phase and must not supply GREEN/RED
evidence. Adopted conflict runs remain foreign-owned and are never cancelled.
During `agent-inspect`, `inspect`, and `inspect-closeout`, problems retrieval reuses the trigger's
scope, unversioned-file policy, changed-files mode, profile, and targeted file
selectors rather than reconstructing a broader request.
If the reason is `ide_selection_required`, `ide_config_ambiguous`, or
`ide_config_missing`, say directly that the repo needs preferred JetBrains IDE
metadata in `.github/github.json`; do not frame that as merely optional when
the same repo will be inspected again.
The helper appends each `UNKNOWN` verdict to
`${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/jetbrains-inspection/unknown-verdicts.jsonl`
so repeated blockers can be fixed later. Set `JB_INSPECT_UNKNOWN_LOG=0` to
disable logging, set it to a path to override the log file, or set
`JB_INSPECT_ROLLOUT_FILE` to include the current rollout/session transcript in
the record. Set `JB_INSPECT_DEPLOYMENT_MANIFEST` to the immutable deployment or
runtime-reconciliation manifest used for qualification; its content SHA-256
takes precedence over rollout-file fallback. JSONL appends are locked,
complete short writes, and roll back failed writes; a malformed persisted row
still fails strict qualification. New outcome rows use schema version 2 with a unique event ID,
assessment/observation kind, assessment ID copied only from `client_run_id`,
millisecond timestamp, final evidence IDs, repo/worktree/project hashes, repo
HEAD, canonical scope descriptor/hash, full helper content SHA-256, exact
plugin version/fingerprint, authoritative IDE product/version/channel,
deployment-manifest content SHA-256, inspection-started state, cleanup, and
ordered internal-attempt summaries. Durable rows hash local paths and project
keys and redact token-like fields and path tokens in diagnostic prose.

## Strict Outcome Qualification

Use strict mode only with an explicit schema-v1 qualification file:

```json
{
  "schema_version": 1,
  "boundary": {
    "since": "2026-07-26T00:00:00.000Z",
    "after_event_id": "optional-boundary-event-id"
  },
  "helper_revision": "sha256:<64 hex characters>",
  "plugin_build_fingerprint": "<exact full-commit fingerprint>",
  "deployment_manifest_sha256": "sha256:<64 hex characters>"
}
```

Strict mode considers only schema-v2 `inspection_assessment` events after the
boundary and groups them deterministically by assessment ID. The gate freezes
at the first requested number of qualifying assessments so later log appends
cannot rewrite that sample; post-sample failures remain separately visible. It
records internal retry attempts inside their single terminal assessment event;
distinct terminal events cannot reuse one assessment ID, and later invocations
cannot rewrite a frozen sample. Event IDs seen before the boundary are retained
for replay detection, so copied post-boundary rows are excluded rather than
counted again. Helper-opened projects
intentionally left open with `--keep-warm` record `cleanup=kept_warm` and cannot
qualify as cleanup-clean evidence. It
reports every post-boundary exclusion, exact duplicate, repeated repo/project
concentration, ordered attempts, UNKNOWN-to-decisive recovery,
verdict/classification/phase/cleanup rollups, decisive rate, and remaining
sample count. A configuration-
blocked event is a harmless exclusion only when `inspection_started=false` and
the exact `ide_selection_required`, `ide_config_ambiguous`, or
`ide_config_missing` code is recorded at phase `selection`. Missing provenance,
artifact mismatch, attribution mismatch, unattributed UNKNOWN, non-clean
cleanup, conflicting decisive outcomes, invalid configuration-blocked rows, or
hidden terminal failures inside the frozen sample window fail the gate. `pass`
requires the requested sample size, at least 95% decisive, and zero hard
failures; otherwise the gate is `incomplete` or `fail`. If a log contains
malformed rows, provide `boundary.after_event_id` so the helper can prove which
side of the boundary they occupy.

- `scope_semantic_coverage_missing` is `UNKNOWN`: one or more requested scoped
  files resolved only as generic TextMate/PlainText PSI, were invalid, or were
  outside project content. This overrides an otherwise clean or plugin-provided
  `GREEN`, including mixed-language scopes where only some files have semantic
  support. `in_source: false` alone is not a failure because language-aware PSI
  can inspect valid project-content files outside a configured source root.
  Valid in-content JetBrains project metadata identified by the authoritative
  `IDEA_MODULE` file role is classified as `project_metadata`, even when the IDE
  exposes it as `PsiPlainTextFile`; it does not require language-aware PSI. The
  helper reports that classification with no-action guidance instead of
  suggesting a language plugin. A recognized dependency lockfile may likewise
  be classified as `excluded_dependency_lockfile`, but only when the plugin
  reports `is_excluded: true` and the matching stable role. A basename without
  explicit IDE exclusion, an unknown role, or a blanket `*.lock` pattern stays
  fail-closed. When a file is outside project content, the
  next action points to the intended module/content root rather than language
  support. Otherwise, install or enable the needed language plugin, select a
  compatible IDE, or update the repo's preferred IDE metadata before rerunning.
  Use `--allow-text-only-coverage` only when generic text coverage is intentionally
  sufficient; it does not allow invalid files or files outside project content.
  Check the selected files before starting a readiness assessment. When every
  target is intentionally generic data, schema, or text and semantic PSI is not
  expected or required, include the override on the first assessment rather
  than generating a post-start configuration failure and rerunning. Never use
  the override for source code or a mixed scope containing source code.
  For older plugin builds that left this settled state waiting until timeout,
  the helper accepts the explicit override only when the same-run snapshot is
  clean, complete, current, and blocked solely by `non_semantic_fallback`.
  Generic, indexing, stale, incomplete, or contradictory timeouts remain
  `UNKNOWN`.
  The helper preserves actionable `RED` findings while attaching the semantic
  coverage gap so real findings are not hidden.
- `scope_semantic_coverage_truncated` is `UNKNOWN` for an otherwise clean result:
  the plugin resolved more files than it proved through either detailed rows or
  the aggregate semantic-coverage summary, so the helper cannot prove complete
  scope coverage. Bounded detail rows alone are not truncation when the
  aggregate summary proves every resolved file and preserves missing-coverage
  counts/examples. Text-only allowance does not override genuinely unproven
  files; update the plugin/helper proof path and rerun. Actionable current RED
  findings remain visible with the coverage gap attached.
