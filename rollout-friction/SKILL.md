---
name: rollout-friction
description: Use only when the user explicitly asks to audit rollout/session files, runout files, session traces, or agent workflow friction. Never use implicitly or for ordinary debugging.
metadata:
  short-description: Audit rollout traces for workflow friction
policy:
  allow_implicit_invocation: false
resources:
  - path: scripts/analyze_rollouts.py
    kind: script
    description: Deterministic local rollout/session friction analyzer.
  - path: scripts/segment_rollout_episodes.py
    kind: script
    description: Group deterministic friction hits into costed episodes with basic outcome classification.
  - path: scripts/cluster_rollout_episodes.py
    kind: script
    description: Cluster friction episodes and emit compact trajectory skeletons for human or model review.
  - path: scripts/classify_auto_review_ledger.py
    kind: script
    description: Classify Every Code auto-review ledger entries against the active checkout so stale detached proposal findings are not treated as current blockers.
  - path: scripts/extract_rollout_memory.py
    kind: script
    description: Legacy broad extraction of destination-aware durable-memory candidates from local rollout/session traces.
  - path: scripts/review_rollout_memory_batches.py
    kind: script
    description: Run trusted-local LLM review over extractor prompt batches with local-llm API lifecycle support and validate coverage.
  - path: scripts/reduce_rollout_memory_reviews.py
    kind: script
    description: Reduce strict-valid local LLM review outputs into a draft apply plan.
  - path: scripts/prepare_rollout_memory_long_context_review.py
    kind: script
    description: Prepare selected-note long-context review prompts and normalize duplicated structured-output captures.
  - path: scripts/run_rollout_memory_long_context_matrix.py
    kind: script
    description: Run stdout-only selected-note review checks across model, budget, and provider variants.
  - path: scripts/lm_studio_scout.py
    kind: script
    description: Run a bounded trusted-local LM Studio scout pass over redacted rollout analysis.
  - path: scripts/benchmark_lm_studio.py
    kind: script
    description: Benchmark local LM Studio models for machine-local rollout review diagnostics.
  - path: scripts/validate_rollout_memory_llm_results.py
    kind: script
    description: Validate strict JSON and candidate coverage for local LLM memory-review results.
commands:
  - name: analyze-rollouts
    source: skill
    resource_path: scripts/analyze_rollouts.py
    example_argv:
      [
        "uv",
        "run",
        "rollout-friction/scripts/analyze_rollouts.py",
        "--root",
        "~/.code/sessions",
      ]
    purpose: Analyze local rollout/session traces for workflow friction signals.
  - name: segment-rollout-episodes
    source: skill
    resource_path: scripts/segment_rollout_episodes.py
    example_argv:
      [
        "uv",
        "run",
        "rollout-friction/scripts/segment_rollout_episodes.py",
        "--paths-file",
        ".local/rollout-friction/<run-id>/paths.txt",
      ]
    purpose: Convert friction signal hits into JSONL episodes with cost and outcome metadata.
  - name: cluster-rollout-episodes
    source: skill
    resource_path: scripts/cluster_rollout_episodes.py
    example_argv:
      [
        "uv",
        "run",
        "rollout-friction/scripts/cluster_rollout_episodes.py",
        ".local/rollout-friction/<run-id>/episodes.jsonl",
      ]
    purpose: Cluster episodes and produce compact redacted trajectory skeletons for targeted review.
  - name: classify-auto-review-ledger
    source: skill
    resource_path: scripts/classify_auto_review_ledger.py
    example_argv:
      [
        "uv",
        "run",
        "rollout-friction/scripts/classify_auto_review_ledger.py",
        ".local/rollout-friction/<run-id>/auto-review-ledger.txt",
        "--repo",
        ".",
        "--json",
      ]
    purpose: Distinguish current-target auto-review findings from stale detached auto-review proposal diagnostics.
  - name: rollout-local-scout
    source: skill
    resource_path: scripts/lm_studio_scout.py
    example_argv:
      [
        "uv",
        "run",
        "rollout-friction/scripts/lm_studio_scout.py",
        ".local/rollout-friction/<run-id>/clusters.json",
        "--role",
        "rollout_scout",
        "--load-policy",
        "jit_chat",
        "--ttl",
        "300",
        "--warmup",
        "--json",
      ]
    purpose: Run one bounded direct local LLM scout over redacted rollout-friction output using shared local-llm lifecycle mechanics.
  - name: extract-rollout-memory
    source: skill
    resource_path: scripts/extract_rollout_memory.py
    example_argv:
      [
        "uv",
        "run",
        "rollout-friction/scripts/extract_rollout_memory.py",
        "--root",
        "~/.code/sessions",
        "--trusted-originals",
        "--output-dir",
        ".local/rollout-memory/<run-id>",
      ]
    purpose: Legacy broad candidate extraction for explicit durable-memory review, not the default friction-audit path.
  - name: review-rollout-memory-batches
    source: skill
    resource_path: scripts/review_rollout_memory_batches.py
    example_argv:
      [
        "uv",
        "run",
        "rollout-friction/scripts/review_rollout_memory_batches.py",
        ".local/rollout-memory/<run-id>/llm-prompts.jsonl",
        "--role",
        "rollout_memory_review",
        "--load-policy",
        "api_explicit",
        "--unload-after",
        "--warmup",
        "--output-dir",
        ".local/rollout-memory/<run-id>/reviews",
      ]
    purpose: Review extractor prompts with a trusted local model and validate per-batch coverage.
  - name: reduce-rollout-memory-reviews
    source: skill
    resource_path: scripts/reduce_rollout_memory_reviews.py
    example_argv:
      [
        "uv",
        "run",
        "rollout-friction/scripts/reduce_rollout_memory_reviews.py",
        ".local/rollout-memory/<run-id>/reviews",
        "--output",
        ".local/rollout-memory/<run-id>/apply-plan.json",
      ]
    purpose: Build a local draft apply plan, including a curated shortlist, from strict-valid review batches.
  - name: prepare-rollout-memory-long-context-review
    source: skill
    resource_path: scripts/prepare_rollout_memory_long_context_review.py
    example_argv:
      [
        "uv",
        "run",
        "rollout-friction/scripts/prepare_rollout_memory_long_context_review.py",
        "prepare",
        ".local/rollout-memory/<run-id>/llm-prompts.jsonl",
        "--budget",
        "quarter",
      ]
    purpose: Build selected-note prompt payloads for approved long-context comparison runs.
  - name: run-rollout-memory-long-context-matrix
    source: skill
    resource_path: scripts/run_rollout_memory_long_context_matrix.py
    example_argv:
      [
        "uv",
        "run",
        "rollout-friction/scripts/run_rollout_memory_long_context_matrix.py",
        ".local/rollout-memory/<run-id>/llm-prompts.jsonl",
        "--dry-run",
        "--budget",
        "quarter",
        "--variant",
        "gpt-5.4=code-llm:gpt-5.4",
        "--output-jsonl",
        ".local/rollout-memory/<run-id>/matrix-results.jsonl",
      ]
    purpose: Produce JSONL status rows for approved model/budget comparison runs, including blocked access, timeout, budget, and validation outcomes.
---

# Rollout Friction

Use this skill only when the user explicitly invokes it or clearly asks to audit
rollout files, session traces, runout files, or agent workflow friction.

## Hard Gates

- Do not use this skill implicitly. If the user did not explicitly ask for
  rollout/session friction analysis, do not load or apply this workflow.
- Start in read-only mode: inspect traces, classify signals, and propose changes
  first.
- Do not edit, delete, move, archive, upload, summarize into public docs, or
  commit raw rollout/session files unless the user explicitly approves that exact
  action.
- Treat approval as scoped. Approval to analyze traces does not authorize skill,
  harness, memory, issue, repo, or local-config changes.
- Reconfirm approval when a proposed action changes destination. For example,
  approval to draft a skill change does not authorize a harness patch or local
  config edit.
- Never put secrets, credentials, private hostnames, customer/client data,
  private message contents, local-only paths, machine-specific values, or
  personal account details into public skills or repo docs.

## Source Roles

- **Rollout/session files**: private short-term evidence of what happened during
  an agent session. They are diagnostic inputs, not durable truth.
- **Friction findings**: local summaries of repeated failures, confusion, tool
  pressure, or workflow drag. They need human review before promotion.
- **Skills**: public, durable agent behavior and reusable workflows.
- **Harness/code changes**: durable fixes when the environment can make the
  correct path easier or harder to misuse.
- **Local config**: private account, token, path, machine, or repo preference
  data. Prefer gitignored config for anything not safe to publish.
- **People local config**: when the optional `people` skill and
  `.local/people.yaml` are available, use them as maintained private context for
  identity, aliases, bot aliases, handles, contact surfaces, actor trust hints,
  and role hints while classifying person-related friction.

## Audit Workflow

1. Identify the relevant trace sources. Prefer recent, scoped rollout/session
   files over broad historical scans.
2. Run `uv run rollout-friction/scripts/analyze_rollouts.py` with explicit paths
   or an explicit bounded `--root`. For many recent files, write the paths to a
   newline- or NUL-delimited file and pass `--paths-file`; do not pass one
   space-joined path string. Keep `--max-files`, total `--max-bytes`, and
   optional `--max-file-bytes` bounded, and read `scan_limitations` separately
   from findings when judging degraded scans. Keep analysis local.
3. Run `segment_rollout_episodes.py` over the same bounded source set to turn
   line-level signal hits into costed friction episodes. Episodes are the review
   unit: they preserve the intent-to-outcome shape better than individual
   snippets while still avoiding raw trace dumps.
4. Run `cluster_rollout_episodes.py` on the episode JSONL to collapse recurring
   patterns into root-cause clusters and compact trajectory skeletons. Review
   high-cost clusters before inspecting raw trace snippets. Skeleton output is
   redacted by default; pass `--trusted-originals` only for approved local-only
   review where path/email/id shapes are useful. Treat hit counts as triage, not
   as proof that thousands of durable lessons exist.
5. When session context includes an Every Code auto-review ledger or repeated
   warnings about generated detached `auto-review-<hex>` worktrees, save the ledger text to an
   ignored local file and run `classify_auto_review_ledger.py` against the active
   repo. Default JSON redacts raw local finding locations and titles into stable
   ids; use `--trusted-local-details` only for approved local-only diagnosis.
   Treat `current_target` findings as review evidence to address or explicitly
   defer even if the review ran in a detached generated worktree. Treat
   `detached_auto_review` findings as external proposal history by default only
   when the snapshot does not match the active target; verify against active
   `HEAD` before letting them block the current task. Do not read, delete, or
   modify detached auto-review worktrees as part of this classification.
6. If a local LLM is useful, give it trajectory skeletons or redacted analyzer
   output, not broad memory-candidate batches. Use it only as a private bounded
   scout. When the
   optional `local-llm` skill is available, first resolve the endpoint locality
   and trust from local config or inventory:
   - For trusted `localhost` or trusted-LAN endpoints, original private local
     rollout snippets may be sent to the model for the current task. Keep the
     run bounded, strip obvious secrets such as tokens/passwords/API keys, and
     store any raw prompts or outputs only in ignored local files when needed.
   - For cloud, unknown, disabled, or untrusted endpoints, give only redacted
     analyzer output, signal names, and short synthesized observations, not raw
     traces.
     Use the model index and shared local-LLM lifecycle helper for
     endpoint/model selection, JIT loading, warm-up, TTL, and bounded chat
     mechanics, while keeping this skill's rollout-specific evidence rules. Use
     deep or cold-load model roles only for deliberate large-model reviews, not
     ordinary audits. Prefer
     `uv run rollout-friction/scripts/lm_studio_scout.py <redacted-report> --role rollout_scout --load-policy jit_chat --ttl 300 --warmup`
     when LM Studio is available. Run at most one scout pass unless the user asks
     for another.
     Ask for missing classes or false-positive patterns, then verify every
     suggestion yourself against maintained sources before acting.
7. Classify each cluster or high-cost episode as one of:
   - `promote-to-skill`
   - `fix-script-or-helper`
   - `fix-harness`
   - `move-to-local-config`
   - `investigate-repo-workflow`
   - `ignore-noise`
8. Verify any durable recommendation against maintained sources before proposing
   it. Examples: local code, skill files, repo docs, GitHub state, harness code,
   official docs, or config schemas.
9. Present a concise proposal with the cluster, evidence summary, likely cause,
   recommended destination, and exact changes that would need approval.
10. Ask for explicit human approval before making any changes.

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

For rollout/model matrix evaluation, use the provided rollout-friction scripts
and their script-owned one-shot transports. For `code-llm` variants, that means
the strict `code llm request --message-file` path; other matrix providers must
stay behind the matrix runner's bounded provider-specific transport. Do not use
`agent.create` `context_files` to pass rollout prompt payloads to agents for
matrix/model evaluation.

`context_files` snapshots file contents directly into a spawned agent prompt.
Use it only for deliberate agent-context snapshots, and require an explicit
large `context_budget_tokens` when a large file is truly intended. For rollout
evaluation, prefer `run_rollout_memory_long_context_matrix.py` so prompt content,
budgets, validation, and output artifacts stay on the controlled one-shot path.

The trusted-local batch review path is different: `review_rollout_memory_batches.py`
may send approved prompt content directly in the local OpenAI-compatible request
body to a trusted localhost or trusted-LAN model. Keep that local-review path
bounded with `--max-input-chars` and do not substitute remote agents or
`context_files` for it.

## Friction Signals

Look for concrete patterns, not vibes:

- repeated failed commands or tool calls
- retries of the same or very similar operation
- GitHub REST or GraphQL rate-limit pressure
- helpers bypassed when a skill says to prefer them
- helper routing stalls or repeated delegation without new state
- local LLM scout drift, timeouts, or unbounded trace requests
- login/auth/account-state loops that explain apparent tool failures
- local config/schema drift that makes agents rediscover private setup
- people/identity friction such as stale handles, wrong manager routing,
  unknown actors, bot ownership confusion, reviewer/assignee/contact confusion,
  or repeated corrections about who a named person is
- stale IDE inspection or readiness results
- validation gates that start but do not reach an observable pass/fail state
- auto-review loops, stale worktree findings, or rejected findings recurring
- stale duplicate edits left in the original checkout after clean-worktree PRs
- worktree lock/contention or cleanup waits that block useful progress
- long status-polling loops
- status polling whose backoff grows without a state change
- user corrections that indicate context drift or forgotten intent
- repeated planning without new execution
- missing tool/config/dependency failures
- high token-growth or duplicate-item warnings when present

## Promotion Rules

- Promote to a skill only when the fix is reusable, public-safe, durable, and
  procedural.
- Fix a script/helper when the right path can be automated or made harder to
  misuse.
- Fix the harness when the environment can expose clearer telemetry, prevent a
  repeated trap, or route agents to the right tool.
- Move to local config when the detail is private, account-specific,
  machine-specific, or repo-specific but not suitable for public skills.
- Move durable person identity, alias, bot alias, contact, company, trust/posture,
  or relationship facts to people local config when that contract is available.
  Rollout friction may detect wrong-person patterns, but it should not become
  the identity source.
- Ignore one-off failures unless they reveal a broader workflow flaw.
- Treat login/auth/account-state failures as explanatory context first. Promote
  them only when a reusable diagnostic or safer harness state would prevent the
  same investigation loop; otherwise keep them in local config or ignore them as
  resolved environment noise.
- Treat local LLM scout output as a hypothesis source, not evidence. Do not let
  a scout choose public routing, policy, or promotion without local verification.

## When Not To Scout

- Skip or further bound the local LLM scout when even a trusted local prompt
  would include secrets, regulated data, or third-party material that should not
  enter model runtime/logging. For untrusted/cloud endpoints, skip when the
  redacted report still contains sensitive client/customer identifiers, private
  project names, or compliance-sensitive details.
- Skip the scout for straightforward findings that can be classified directly
  from analyzer output and maintained sources.
- If LM Studio is unavailable or times out, continue with analyzer output and
  human review. Do not retry repeatedly unless the user explicitly wants an LM
  Studio diagnostic pass.
- Prefer `uv run local-llm/scripts/lm_studio_benchmark.py --role rollout_scout`
  for local setup diagnostics. Benchmark results are machine-local evidence,
  not public skill content.

## Reporting

Keep reports compact and redacted:

- What trace source was audited.
- What friction signals were found.
- What evidence supports each signal, without raw private transcript dumps.
- What durable destination is recommended.
- What changes require explicit approval.
- What remains unknown or risky.
