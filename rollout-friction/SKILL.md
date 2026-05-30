---
name: rollout-friction
description: Use only when the user explicitly asks to audit rollout/session files, runout files, session traces, or agent workflow friction. Never use implicitly or for ordinary debugging.
metadata:
  short-description: Audit rollout traces for workflow friction
policy:
  allow_implicit_invocation: false
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

## Audit Workflow

1. Identify the relevant trace sources. Prefer recent, scoped rollout/session
   files over broad historical scans.
2. Run `uv run rollout-friction/scripts/analyze_rollouts.py` with explicit paths
   or an explicit bounded `--root`. Keep analysis local.
3. Review the findings and inspect only the minimum raw trace snippets needed to
   understand high-value signals.
4. If a local LLM is useful, use it only as a private bounded scout. Give it
   redacted analyzer output, signal names, and short synthesized observations,
   not raw traces. Prefer
   `uv run rollout-friction/scripts/lm_studio_scout.py <redacted-report>` when
   LM Studio is available; the helper defaults to a small bounded local model,
   can be pointed at a newer local model with `ROLLOUT_FRICTION_LM_MODEL`,
   strips known response wrappers, and times out instead of looping forever. Use
   `--deep` only for deliberate large-model or cold-load reviews, not ordinary
   audits. Treat these helper scripts as a temporary bridge until Every Code has
   first-class LM Studio support. Run at most one scout pass unless the user asks
   for another. Ask for missing classes or false-positive patterns, then verify
   every suggestion yourself against maintained sources before acting.
5. Classify each finding as one of:
   - `promote-to-skill`
   - `fix-script-or-helper`
   - `fix-harness`
   - `move-to-local-config`
   - `investigate-repo-workflow`
   - `ignore-noise`
6. Verify any durable recommendation against maintained sources before proposing
   it. Examples: local code, skill files, repo docs, GitHub state, harness code,
   official docs, or config schemas.
7. Present a concise proposal with the signal, evidence summary, likely cause,
   recommended destination, and exact changes that would need approval.
8. Ask for explicit human approval before making any changes.

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
- Ignore one-off failures unless they reveal a broader workflow flaw.
- Treat login/auth/account-state failures as explanatory context first. Promote
  them only when a reusable diagnostic or safer harness state would prevent the
  same investigation loop; otherwise keep them in local config or ignore them as
  resolved environment noise.
- Treat local LLM scout output as a hypothesis source, not evidence. Do not let
  a scout choose public routing, policy, or promotion without local verification.

## When Not To Scout

- Skip the local LLM scout when the redacted report still contains sensitive
  client/customer identifiers, private project names, or compliance-sensitive
  details that should not enter a model prompt.
- Skip the scout for straightforward findings that can be classified directly
  from analyzer output and maintained sources.
- If LM Studio is unavailable or times out, continue with analyzer output and
  human review. Do not retry repeatedly unless the user explicitly wants an LM
  Studio diagnostic pass.
- Use `uv run rollout-friction/scripts/benchmark_lm_studio.py` only for local
  LM Studio setup diagnostics. Benchmark results are machine-local evidence, not
  public skill content.

## Reporting

Keep reports compact and redacted:

- What trace source was audited.
- What friction signals were found.
- What evidence supports each signal, without raw private transcript dumps.
- What durable destination is recommended.
- What changes require explicit approval.
- What remains unknown or risky.
