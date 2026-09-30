# Rollout Memory Extraction And Model Matrices

Read this reference before explicit durable-memory extraction or rollout/model
matrix evaluation. Ordinary friction audits use the entry point's episode and
cluster workflow. These procedures prepare review artifacts; they do not apply
memory updates or expand approval.

- [Memory extraction](#memory-extraction-workflow)
- [Long-context prompt path](#long-context-prompt-path)

## Memory Extraction Workflow

Use this only after explicit approval to inspect rollout/session traces for
durable memory candidates. This legacy broad-extraction workflow prepares review
artifacts; it does not apply memory updates by itself. Prefer the episode and
cluster workflow above for ordinary rollout-friction audits; use broad memory
extraction only when the user explicitly asks to mine rollout traces for durable
memory/profile/local-config candidates.

1. Run `extract_rollout_memory.py` with explicit time/file bounds and an ignored
   `.local/rollout-memory/<run-id>/` output directory. Use `--trusted-originals`
   only for localhost or trusted-LAN models approved for private local inputs;
   use `--redact` for cloud, unknown, disabled, or untrusted endpoints. Redacted
   extraction strips obvious secrets, local paths, and person identifiers such as
   natural names, handles, and emails from candidate text and prompts.
2. Prefer destination-filtered passes when applying memory. Review `people`,
   `profile`, and `local-llm` separately from `repo-specific` and
   `rollout-friction` candidates so repo details do not pollute central memory.
3. Tune `--batch-chars` and `--max-record-chars` from a small calibration run.
   Oversharded prompts lose synthesis value, while overlarge prompts are more
   likely to truncate or omit candidate IDs. Validate with
   `validate_rollout_memory_llm_results.py` before scaling.
4. Use `review_rollout_memory_batches.py` only against trusted local/private
   endpoints. Resolve endpoint, role, model, TTL, and context through the
   `local-llm` skill's API-first lifecycle. For broad extraction batches,
   prefer `--role rollout_memory_review --load-policy api_explicit --warmup
   --unload-after` so the context/load parameters are explicit and the warm-up
   sends only harmless text before private rollout prompts. Use JIT+TTL for
   smaller scout passes, not broad memory-review batches. Use
   `--split-on-failure` when malformed or incomplete batches need deterministic
   child-batch retries.
5. Apply nothing from a batch that fails strict JSON or candidate coverage until
   it is rerun, split, or manually reviewed.
6. Run `reduce_rollout_memory_reviews.py` only after strict validation. Treat the
   reducer output as an apply-plan draft. Start manual review from
   `curated_shortlist`, then inspect full destination buckets only when the
   shortlist reveals a useful theme. The shortlist is advisory, not an auto-apply
   list; inspect suggested updates before editing `.local/profile.md`,
   `.local/people.yaml`, `.local/local-llm.yaml`, skills, or repo files.
   When the apply plan or temporary artifacts include `people_updates`,
   `people_resolver_smoke_checks`, visible person names, handles, aliases,
   reviewer/assignee/manager fields, or contact/routing notes, invoke the
   `people` skill's artifact review workflow before closeout: search the local
   artifacts for every known alias/handle form, inspect smoke checks, and verify
   natural names resolve before considering people-memory work complete.
7. For explicitly approved cloud or long-context comparison tests, use
   `prepare_rollout_memory_long_context_review.py` to build selected-note prompts
   with a `candidate_id_manifest`. Validate outputs with
   `validate_rollout_memory_llm_results.py --allow-implicit-discards`; this mode
   still requires every candidate in `reviewed_candidate_ids` and treats omitted
   reviewed candidates as implicit discards.
8. Use `run_rollout_memory_long_context_matrix.py --dry-run` before full matrix
   tests. For real approved cloud tests, pass both `--allow-private-cloud` and
   `--confirm-private-provider <provider>` for each provider that may receive
   private prompt content in that run. Capture stdout JSONL under `.local/`; use
   `--output-dir` for normalized per-row cloud artifacts that need later
   qualitative comparison. Pass `--output-jsonl` with `--skip-existing` for
   resumable runs. By default, resumable runs skip only existing `passed` rows;
   use `--skip-status` only when intentionally preserving another status, and
   `--retry-status` when rerunning a previously skipped status after an access
   window or harness fix. Treat statuses such as
   `prompt_too_large`, `blocked_access`, `blocked_transport`, `budget_exceeded`,
   `timeout`, and `failed_validation` as first-class results to retry or fix, not
   as successful reviews.

## Long-Context Prompt Path

For rollout/model matrix evaluation, use the provided scripts and their bounded,
provider-specific one-shot transports: `code-llm` variants use strict
`code llm request --message-file`. Do not use `agent.create` `context_files`
for rollout prompt payloads in matrix/model evaluation.

`context_files` snapshots file contents directly into a spawned agent prompt.
Use it only for deliberate agent-context snapshots, with an explicit large
`context_budget_tokens` when a large file is intended.

Trusted-local batch review instead uses `review_rollout_memory_batches.py` to
send approved content directly in the local OpenAI-compatible request body.
Bound it with `--max-input-chars`; do not substitute remote agents or
`context_files` for this path.

For GPT-5.6 migration comparisons, add explicit Sol, Terra, or Luna variants
with `--variant` alongside the existing GPT-5.4 comparison instead of replacing
the pinned baseline. Keep new family variants opt-in: every additional variant
changes provider cost and runtime, and Sol should not become the default for
every workload. Preserve fake `gpt-5.1-codex` harness models because they are
deterministic protocol fixtures rather than production recommendations.

