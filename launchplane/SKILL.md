---
name: launchplane
description: Use for Launchplane-managed product/runtime state, secrets, config, deployments, rollout direction, product ownership boundaries, merge-train flow, and audited operator mutations. Use with github-plan when Launchplane work needs to stay aligned with a durable plan, issue graph, blockers, or rollout sequence. If authority is unknown or discovering private infrastructure access, use docs-lookup first.
metadata:
  short-description: Operate Launchplane-managed state
resources:
  - path: scripts/launchplane-context.py
    kind: script
    description: Read Launchplane context for a repository, branch, issue, or pull request.
  - path: scripts/launchplane-write-action.py
    kind: script
    description: Perform bounded Launchplane write-action preflight, dry-run, apply, and merge-train controller calls.
  - path: references/context-helper-contract.md
    kind: reference
    description: Contract for Launchplane context helper configuration, fallback, output, and redaction behavior.
  - path: references/operator-contract.md
    kind: reference
    description: Operator safety contract for Launchplane private config, credentials, and runtime mutations.
  - path: references/write-action-helper-contract.md
    kind: reference
    description: Contract for write-action helper entrypoints, exit behavior, idempotency, and redacted output.
  - path: references/public-safety.md
    kind: reference
    description: Public-safety guidance for Launchplane outputs, repo metadata, and credential handling.
  - path: references/context.available.example.json
    kind: reference
    description: Example available Launchplane context response.
  - path: references/context.no-context.example.json
    kind: reference
    description: Example no-context Launchplane response.
  - path: references/launchplane-context.local.example.json
    kind: reference
    description: Public-safe example for private context helper configuration.
  - path: references/launchplane-operator.local.example.json
    kind: reference
    description: Public-safe example for private operator helper configuration.
commands:
  - name: launchplane-context
    source: skill
    resource_path: scripts/launchplane-context.py
    example_argv:
      ["uv", "run", "scripts/launchplane-context.py", "--repo", "OWNER/REPO"]
    purpose: Reads Launchplane context through the structural helper.
  - name: launchplane-product-config-preflight
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "product-config-preflight",
        "--product",
        "<product>",
        "--context",
        "<context>",
        "--source-url",
        "<url>",
        "--reason",
        "<reason>",
      ]
    purpose: Preflights product-config intent through the bounded helper path.
  - name: launchplane-product-config-dry-run
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "product-config-dry-run",
        "--payload-file",
        "<file>",
      ]
    purpose: Performs a redacted product-config dry-run before any apply.
  - name: launchplane-product-config-apply
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "product-config-apply",
        "--payload-file",
        "<file>",
        "--idempotency-key",
        "<key>",
      ]
    purpose: Applies product-config changes through the bounded helper after approval.
  - name: launchplane-merge-train-controller-run-once
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "merge-train-controller-run-once",
        "--repo",
        "OWNER/REPO",
        "--idempotency-key",
        "<key>",
      ]
    purpose: Advances one merge-train controller phase through the bounded helper.
  - name: launchplane-operator-config-diagnostic
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "operator-config-diagnostic",
      ]
    purpose: Reports redacted operator URL/token source presence before write-capable helper calls.
policy:
  command_policies:
    - id: prefer-launchplane-write-helper-for-product-config-api
      match:
        shell_regex: "\\b(curl|wget|http)\\b.*\\b/v1/(product-config/apply|agent/write-intents/evaluate)\\b"
      action: require_preferred
      message: Raw Launchplane product-config API calls bypass helper-owned dry-run/apply discipline, redaction, private config sourcing, and traceable operator output. Use the write-action helper.
      preferred:
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-write-action.py",
              "product-config-preflight",
              "--help",
            ]
          purpose: Preflights product-config intent through the bounded helper path.
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-write-action.py",
              "product-config-dry-run",
              "--payload-file",
              "<file>",
            ]
          purpose: Performs redacted product-config dry-run before any apply.
    - id: prefer-launchplane-write-helper-for-merge-train-api
      match:
        shell_regex: "\\b(curl|wget|http)\\b.*\\b/v1/work-graph/merge-train/controller/run-once\\b"
      action: require_preferred
      message: Raw Launchplane merge-train controller calls bypass helper-owned config, idempotency, redacted output, and phase evidence. Use the write-action helper.
      preferred:
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-write-action.py",
              "merge-train-controller-run-once",
              "--help",
            ]
          purpose: Advances the merge train through the bounded controller helper.
    - id: prefer-launchplane-helpers-over-global-cli
      match:
        argv_prefix: ["launchplane"]
      action: require_preferred
      message: Do not assume a global `launchplane` binary on ordinary workstations. Use the bundled helpers unless you are explicitly on a host-only Launchplane context with a repo-provided command.
      preferred:
        - kind: script
          path: scripts/launchplane-context.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-context.py",
              "--repo",
              "OWNER/REPO",
            ]
          purpose: Reads Launchplane context through the structural helper.
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-write-action.py",
              "merge-train-controller-run-once",
              "--help",
            ]
          purpose: Uses bounded Launchplane mutation entrypoints when operator action is approved.
---

# Launchplane Expert

Use this skill to inspect product/runtime state and perform safe,
authenticated mutations via the Launchplane service API.

Use `docs-lookup` first when a task is discovering the source of truth or access
path for external/private infrastructure and it is not already clear that
Launchplane manages that resource.

## Runtime Authority Boundary

Checked-in files are not runtime authority for Launchplane-managed state. Code
may own schemas, validators, generic behavior, helper routing, fake examples,
and fail-closed defaults. Launchplane service records or explicit scoped
operator input own real product, tenant, repository, branch, domain, lane,
provider-target, runtime-environment, authz, operator, route, health-check, and
other mutable runtime values.

This applies even when values are not secrets. Non-secret topology can still
steer production behavior. Treat repo metadata, workflow variables, checked-in
examples, and archived workstation files as hints for which Launchplane helper,
service record, or operator surface to use; never use them as evidence of the
current live value. If the needed live value is only visible in checked-in or
workstation files, stop and obtain Launchplane context or explicit operator
input instead of inferring it.

When a repo has `.github/github.json`, inspect its `launchplane` block before
looking in sibling repos, archived workstation files, or workflow variables. The
repo block is public-safe routing metadata only: it may name helper paths,
environment variable names for service URLs, local config examples,
merge-train labels, and GitHub Actions workflow entrypoints. It must not
contain tokens, secret values, cookies, concrete Launchplane service URLs,
private credential paths, provider payloads, product/runtime endpoints, or
plaintext runtime configuration. Treat Launchplane-managed product, app,
preview, deploy, provider, lane, tenant, and health-check coordinates as service
records, not checked-in repo metadata; if repo metadata and Launchplane service
state disagree, service/operator state wins and the metadata is stale routing
context to fix deliberately.

## Core Goal

Provide situational awareness and safe runtime management. Always favor
service-backed audit trails over local ad-hoc fallbacks.

Do not treat archived workstation files under `~/.config/launchplane/` as the
authority for current Launchplane runtime or product state. Files such as
`service.env`, `dokploy.env`, and `runtime-environments.toml` can be useful
historical clues, but they are not live records. When a task asks about current
product state, use the deployed Launchplane service/API or operator UI first;
use direct database access only from an explicitly approved host-side context.

## Rollout Plan Alignment

For Launchplane rollout, runtime, product-boundary, merge-train, or operator
work, do not continue from the latest operational finding alone. Before the next
slice, state how it fits the active Launchplane plan, issue graph, rollout
sequence, or product ownership boundary.

If an operational finding changes the plan, update the owning GitHub plan issue
or PR before treating the new path as canonical. Prefer explicit blocker,
sub-issue, or related-issue edges over burying direction changes in chat.

When Launchplane work turns into GitHub issue, PR, Actions, review, comment,
commit, or push work, delegate that surface to `github` or `github-plan` before
running commands. Launchplane owns runtime/operator authority; the GitHub skills
own helper-backed GitHub identity, body handling, planning state, and PR
lifecycle behavior.

## Situational Awareness (Context)

Use the context helper to identify product mapping, deploy evidence, and
readiness.

- **Usage**: `uv run scripts/launchplane-context.py --repo OWNER/REPO`
- **Output**: See `references/context.available.example.json` for schema.
- **Reporting**: Report readiness, blockers, and next action based on context.
- **Contract**: See `references/context-helper-contract.md` for config,
  fallback, and redaction behavior.

## Runtime Management (Operator)

Mutate runtime environments, managed secrets, and product config.

- **Safety**: Strictly follow the `references/operator-contract.md`.
- **Helper Contract**: Use `references/write-action-helper-contract.md` for
  bounded helper entrypoints, exit behavior, and redacted output shape.
- **Auth**: Prefer signed-in, scoped operator sessions in the Launchplane UI or
  service API. Source terminal/local operator credentials only through the
  operator contract; do not paste token values into chat, issues, PRs, docs, or
  logs.
- **Private Config**: For non-browser terminal execution, use the source order
  in the operator contract. Missing private config means the write-capable path
  is unavailable and must fail closed; do not use `.github/github.override.json`
  for Launchplane credentials.
- **Operator Diagnostics**: Before concluding operator access is unavailable,
  run `scripts/launchplane-write-action.py operator-config-diagnostic`. Treat
  `launchplane-context` availability and local operator readiness as separate
  checks: context can be unavailable while the write helper is usable, and the
  write helper can be blocked only by missing local operator config. If the
  diagnostic reports `missing_service_url`, token material was found but no
  write-capable Launchplane service URL source was found; configure
  `LAUNCHPLANE_OPERATOR_URL` in the private local operator env file or pass
  `--url` before the subcommand, then rerun the diagnostic. If the active shell
  has a service URL under `LAUNCHPLANE_PUBLIC_URL` but not
  `LAUNCHPLANE_OPERATOR_URL`, treat it as an ambiguous URL source: obtain the
  correct operator URL and pass it with `--url` before the subcommand, or copy
  the sanctioned value into private operator config. Do not use public URL
  variables as write authority.
- **Repo Metadata**: Use `.github/github.json` `launchplane` metadata to find
  helper paths, workflow entrypoints, labels, and service URL env var names, but
  keep concrete service URLs and credentials in private operator config,
  environment variables, GitHub Actions OIDC, or signed-in Launchplane UI
  sessions.
- **No Checked-In Product Authority**: Do not add or copy product-specific
  authz grants, provider target route batches, product target IDs, tenant
  domains, runtime seed/import payloads, or live product topology into
  Launchplane deploy scripts, workflow defaults, repo-local config files, or
  product repos. Committed examples must use fake placeholders or intentionally
  public, non-authoritative sample data. For shared/prod, use the deployed
  Launchplane service, operator UI, or the bounded write-action helper/API with
  the correct service URL and scoped credentials.
- **No Checked-In Topology Inference**: Do not infer real products, tenants,
  domains, lanes, provider targets, runtime environments, route batches, authz,
  repository bindings, branch bindings, or operator identity from checked-in
  config or workflow defaults. Those files may identify the Launchplane surface
  to query; they do not answer what live topology is now.
- **First Shot**: For product-config/runtime/secret sync, use the service API
  path from the operator contract first. Do not start by searching for a local
  `launchplane` binary or by poking provider config directly.
- **Denied Actions**: A local operator token can be present and still lack a
  specific action. Report that as authorization denial, not missing credential.
  When a denied action concerns authz grants, provider targets, private health
  endpoints, route records, or other higher-authority runtime records, look for
  the Launchplane authz reconciliation surface first, such as a repo-provided
  deploy workflow or authz-grant reconciliation script running under GitHub
  Actions OIDC. Do not use manual route probes as the next step.
- **Unsupported Helper Coverage**: If the helper lacks a command for the needed
  runtime record workflow, stop at the supported Launchplane service/UI or
  authz reconciliation path. Do not synthesize record payloads from issue text,
  checked-in examples, workflow defaults, provider observations, or local files.
- **Workflow**:
  1. Inspect Context to identify the target and change needed.
  2. Run operator config diagnostics before a write-capable helper call when
     target URL, token source, or authority is unclear.
  3. If diagnostics report `missing_service_url`, fix local operator routing
     first. This is a workstation setup problem, not PR readiness, merge-train
     admission, or scheduler state.
  4. Preflight product-config intent with `scripts/launchplane-write-action.py
product-config-preflight` when agent-side authorization or managed-secret
     binding evidence is useful.
  5. Use the signed-in/scoped operator path when a human-approved runtime or
     managed-secret mutation is required.
  6. Build a product-config request for `POST /v1/product-config/apply` only in
     an approved operator surface. The helper may submit dry-run/apply from a
     private local payload file, never from chat, CLI plaintext secret args, or
     committed examples.
  7. **Dry-run** and inspect redacted results.
  8. **Apply** with a concrete reason only after the dry-run succeeds and the
     operator intent is explicit.
  9. Inspect returned `next_actions` and complete required follow-up actions;
     product-config apply can update Launchplane records before the live target
     runtime has been synced.

Agents may guide the operator, prepare request shape, summarize redacted dry-run
evidence, and report trace IDs/status. Agents must not collect plaintext secret
values in chat, issues, PRs, docs, logs, or helper output, and must not bypass
Launchplane by editing provider configuration directly.

## Merge Train (Controller)

Use Launchplane's controller route as the default merge-train workflow.

- **Preferred Route**: `POST /v1/work-graph/merge-train/controller/run-once`.
- **Helper**: Use `scripts/launchplane-write-action.py
merge-train-controller-run-once` instead of open-coding the route. Mutating
  calls require an idempotency key.
- **Operator Action**: Put `ready-to-merge` only on the root PR that targets the
  protected base branch. Do not hand-collapse stacks in GitHub.
- **Controller Semantics**: Each call advances one safe phase at a time:
  same-repo linear stack-collapse planning/execution when needed,
  collapsed-root admission, candidate plan/build/observe, landing-plan
  creation, PR-native landing, and child PR disposition.
- **Proven Batch Flow**: The controller has been proven against a live
  multi-PR batch train. It can reflow a failed candidate when the eligible queue
  changes, build and observe a replacement candidate, create a landing plan,
  land the original PRs through GitHub's PR merge API in train order, and post
  managed feedback to each PR. Treat this as the normal rollout path, not an
  experimental one-off.
- **Mutation Gate**: Keep scheduled runners in dry-run mode until the operator
  explicitly selects a mutation pilot. Manual `mutate=true` controller runs are
  appropriate only after dry-run evidence shows the intended queue, candidate,
  and next action. Do not leave scheduled mutation enabled as a casual default.
- **Stacked PRs**: For a same-repo linear stack, label only the root PR that
  targets the protected base branch. Let Launchplane collapse child branches
  into that root, wait for the root head SHA to satisfy checks, admit only the
  root to the flat train, and resolve child PRs after the root lands according
  to policy. Treat forked, ambiguous, sibling, cyclic, stale-head, or
  permission-limited stacks as blocked/unsupported instead of mutating by hand.
- **Retry Model**: Repeated controller calls are expected. Stop and report
  blocked, stale, denied, or failed states with compact evidence and trace IDs.
- **Evidence**: For stack runs, report the stack-collapse plan record id, any
  batch candidate record id, the landing-plan record id, workflow run URLs, and
  the final root merge commit. Include child disposition evidence when the root
  lands.
- **Batch Evidence**: For flat batch runs, report the dry-run/admission reason,
  candidate record id and candidate SHA, required-check status on the candidate
  commit, landing-plan record id, each landed PR number and merge commit, managed
  feedback delivery status, and post-merge checks on the target repository's
  default branch.
- **Runtime Checkout Handoff**: After the controller confirms a final landing
  commit, delegate any runtime-bound local checkout reconciliation to `github`
  and its landed repo-local reconciler. Do not reconcile from candidate,
  admission, observation, landing-plan, queued, or other nonterminal controller
  responses. Preserve Launchplane landing success independently when local
  reconciliation is blocked or fails, and block only claims that installed
  runtime behavior is current.
- **Recovery Evidence**: If Launchplane patches are needed during rollout,
  verify their PR checks, post-merge CI/Security/CodeQL, and Deploy Launchplane
  before retrying mutation. Record the failing workflow run id and trace id that
  motivated the patch.
- **Troubleshooting**: Treat phase-specific merge-train endpoints as detail or
  recovery surfaces. They are not the default skill workflow.
- **Boundaries**: Merge-train behavior is DB/policy-backed. Do not hardcode
  repositories, labels, tokens, protected branches, or local file config.

## Intentionality & Safety

This skill combines inspection and mutation. You must explicitly announce when
you are transitioning from **Inspecting Context** to **Executing Operator
Actions**. Never apply a mutation without a preceding dry-run and situational
verification.

## Tools

- `scripts/launchplane-context.py`: Structural state helper.
- `scripts/launchplane-write-action.py`: Public-safe write-action wrapper for
  product-config intent preflight, private local product-config dry-run/apply,
  and merge-train controller calls.
- `operator-config-diagnostic`: Redacted source-presence diagnostic for local
  operator URL and token configuration. Global options such as `--url` must come
  before the subcommand.
- `POST /v1/agent/write-intents/evaluate`: Product-config preflight surface for
  authorization and managed-secret binding evidence; never carries plaintext.
- `POST /v1/product-config/apply`: Primary product-config operator path for
  signed-in/scoped operators; dry-run before apply.
- `POST /v1/work-graph/merge-train/controller/run-once`: Preferred merge-train
  controller path; call repeatedly to advance one safe phase at a time.
- Launchplane host-only CLI helpers: Use only when you are explicitly on the
  Launchplane host via SSH or the repo provides a concrete command. Do not
  assume a global `launchplane` binary exists on ordinary workstations.
