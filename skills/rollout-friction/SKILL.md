---
name: rollout-friction
disable-model-invocation: true
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
  - path: scripts/lm_studio_scout.py
    kind: script
    description: Run a bounded trusted-local LM Studio scout pass over redacted rollout analysis.
  - path: scripts/benchmark_lm_studio.py
    kind: script
    description: Benchmark local LM Studio models for machine-local rollout review diagnostics.
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
        "<sessions-dir>",
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
   files over broad historical scans. Each host keeps its own: Codex writes
   rollout files under its home's `sessions` folder, and Claude Code writes
   transcripts under its `projects` folder. Ask for the location when it is not
   evident; there is no default. The helpers read both hosts' traces and
   classify tool outcomes, including failed results, nonzero exits, and denied
   commands, from each host's structured records.
2. Run `uv run rollout-friction/scripts/analyze_rollouts.py` with explicit paths
   or an explicit bounded `--root`. For many recent files, write the paths to a
   newline- or NUL-delimited file and pass `--paths-file`; do not pass one
   space-joined path string. Keep `--max-files`, total `--max-bytes`, and
   optional `--max-file-bytes` bounded, and read `scan_limitations` separately
   from findings when judging degraded scans. For long scans, use `--progress`
   and an optional cooperative `--max-seconds` budget; keep an outer process
   deadline for operations that cannot be interrupted between checks. Read the
   [budget and count contract](references/event-counts.md) before interpreting
   a time-limited result. Keep analysis local.
3. Run `segment_rollout_episodes.py` over the same bounded source set to turn
   line-level signal hits into costed friction episodes. Episodes are the review
   unit: they preserve the intent-to-outcome shape better than individual
   snippets while still avoiding raw trace dumps. Read
   [event count semantics](references/event-counts.md) before interpreting
   counts, comparing reports, or supplying reports to a model. Version 2 counts
   command results and executed retries; other signal counts remain text
   matches. Nonzero exits and text hints still require human classification.
4. Run `cluster_rollout_episodes.py` on the episode JSONL to collapse recurring
   patterns into root-cause clusters and compact trajectory skeletons. Review
   high-cost clusters before inspecting raw trace snippets. Skeleton output is
   redacted by default; pass `--trusted-originals` only for approved local-only
   review where path/email/id shapes are useful. Treat hit counts as triage, not
   as proof that thousands of durable lessons exist.
5. If a local LLM is useful, give it trajectory skeletons or redacted analyzer
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
6. Classify each cluster or high-cost episode as one of:
   - `promote-to-skill`
   - `fix-script-or-helper`
   - `fix-harness`
   - `move-to-local-config`
   - `investigate-repo-workflow`
   - `ignore-noise`
7. Verify any durable recommendation against maintained sources before proposing
   it. Examples: local code, skill files, repo docs, GitHub state, harness code,
   official docs, or config schemas.
8. Present a concise proposal with the cluster, evidence summary, likely cause,
   recommended destination, and exact changes that would need approval.
9. Ask for explicit human approval before making any changes.

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
