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
  - path: scripts/check-agent-operator-contract.py
    kind: script
    description: Validate the vendored public agent/operator contract and local consumer bindings offline.
  - path: scripts/check-agent-operator-contract-freshness.py
    kind: script
    description: Compare the vendored contract with the current public upstream artifact and report semantic drift.
  - path: references/agent-operator-contract.json
    kind: reference
    description: Vendored public Launchplane agent/operator contract artifact.
  - path: references/agent-operator-contract.md
    kind: reference
    description: Contract identity, validation, freshness, and local-extension guidance.
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
  - name: launchplane-contract-validate
    source: skill
    resource_path: scripts/check-agent-operator-contract.py
    example_argv:
      ["uv", "run", "scripts/check-agent-operator-contract.py"]
    purpose: Proves hermetic consistency between the vendored contract and local Launchplane consumers.
  - name: launchplane-contract-freshness
    source: skill
    resource_path: scripts/check-agent-operator-contract-freshness.py
    example_argv:
      ["uv", "run", "scripts/check-agent-operator-contract-freshness.py", "compare"]
    purpose: Emits advisory current, known-stale, or unknown semantic-freshness evidence.
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
  - name: launchplane-preview-feedback-remediation
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "preview-feedback-remediation",
        "--mode",
        "dry-run",
        "--product",
        "<product>",
        "--context",
        "<context>",
        "--repository",
        "OWNER/REPO",
        "--pull-request-url",
        "<url>",
        "--terminal-status",
        "cleared",
        "--reason",
        "<reason>",
        "--related-issue",
        "OWNER/REPO#123",
        "--idempotency-key",
        "<key>",
      ]
    purpose: Dry-runs or applies one audited Launchplane-managed preview-feedback remediation.
  - name: launchplane-change-impact-policy-dry-run
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "change-impact-policy-dry-run",
        "--payload-file",
        "<private-file>",
      ]
    purpose: Dry-runs an explicit private change-impact policy payload with redacted output.
  - name: launchplane-change-impact-policy-apply
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "change-impact-policy-apply",
        "--payload-file",
        "<private-file>",
        "--reviewed-dry-run",
        "--expected-policy-digest",
        "<dry-run-digest>",
        "--idempotency-key",
        "<key>",
      ]
    purpose: Applies a reviewed private change-impact policy payload with redacted output.
  - name: launchplane-change-impact-policy-read
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "change-impact-policy-read",
        "--repository-id",
        "<repository-id>",
      ]
    purpose: Reads bounded active change-impact policy metadata for verification.
  - name: launchplane-generic-web-deploy-recovery-dry-run
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "generic-web-deploy-recovery-dry-run",
        "--payload-file",
        "<private-file>",
        "--idempotency-key",
        "<original-deploy-key>",
      ]
    purpose: Dry-runs an explicit private generic-web deploy-recovery payload with redacted output.
  - name: launchplane-generic-web-deploy-recovery-apply
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "generic-web-deploy-recovery-apply",
        "--payload-file",
        "<private-file>",
        "--idempotency-key",
        "<original-deploy-key>",
        "--reviewed-dry-run",
        "--expected-recovery-digest",
        "<dry-run-digest>",
        "--dry-run-evidence-file",
        "<private-dry-run-output>",
      ]
    purpose: Applies only apply-eligible reviewed generic-web deploy-recovery evidence with redacted output.
policy:
  command_policies:
    - id: prefer-launchplane-write-helper-for-generic-web-deploy-recovery-api
      match:
        shell_regex: "\\b(curl|wget|http)\\b.*\\b/v1/admin/generic-web/deploy-recovery/(dry-run|apply)\\b"
      action: require_preferred
      message: Raw Launchplane generic-web deploy-recovery calls bypass helper-owned private-file, dry-run/apply, idempotency, digest binding, redaction, and trace discipline. Use the write-action helper.
      preferred:
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-write-action.py",
              "generic-web-deploy-recovery-dry-run",
              "--payload-file",
              "<private-file>",
              "--idempotency-key",
              "<original-deploy-key>",
            ]
          purpose: Dry-runs explicit operator recovery input through the bounded helper.
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-write-action.py",
              "generic-web-deploy-recovery-apply",
              "--payload-file",
              "<private-file>",
              "--idempotency-key",
              "<original-deploy-key>",
              "--reviewed-dry-run",
              "--expected-recovery-digest",
              "<dry-run-digest>",
              "--dry-run-evidence-file",
              "<private-dry-run-output>",
            ]
          purpose: Applies only exact, determinate, retry-safe reviewed recovery evidence through the bounded helper.
    - id: prefer-launchplane-write-helper-for-change-impact-policy-api
      match:
        shell_regex: "\\b(curl|wget|http)\\b.*\\b/v1/change-impact/policies/apply\\b"
      action: require_preferred
      message: Raw Launchplane change-impact policy calls bypass helper-owned private-file, dry-run/apply, idempotency, redaction, and trace discipline. Use the write-action helper.
      preferred:
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-write-action.py",
              "change-impact-policy-dry-run",
              "--payload-file",
              "<private-file>",
            ]
          purpose: Dry-runs explicit operator policy input through the bounded helper.
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
    - id: reject-launchplane-authz-secret-authority
      match:
        shell_regex: "(?i)\\b(?:gh|gh-with-env-token)\\b[\\s\\S]*\\b(?:secret|variable)\\s+set\\b[\\s\\S]*\\blaunchplane_authz_[a-z0-9_]+\\b"
      action: reject
      message: Launchplane authorization desired state must not be created or changed through GitHub secrets or variables. Route the capability gap to the DB-native authorization architecture.
    - id: reject-raw-github-actions-secret-writes
      match:
        shell_regex: "(?i)\\b(?:gh|gh-with-env-token)\\b[\\s\\S]*\\bapi\\b[\\s\\S]*(?:(?:--method|-X)\\s*(?:put|patch|post|delete)\\b[\\s\\S]*/(?:actions/(?:secrets|variables)|environments/[^\\s/]+/(?:secrets|variables))\\b|/(?:actions/(?:secrets|variables)|environments/[^\\s/]+/(?:secrets|variables))\\b[\\s\\S]*(?:--method|-X)\\s*(?:put|patch|post|delete)\\b)"
      action: reject
      message: Raw GitHub Actions secret or variable writes can silently make GitHub an authorization authority. Use the owning product/operator contract; Launchplane auth gaps must escalate to the DB-native authorization architecture.
    - id: reject-http-github-actions-secret-writes
      match:
        shell_regex: "(?i)\\b(?:curl|wget|http)\\b[\\s\\S]*(?:(?:(?:-X|--request|--method(?:=|\\s+))\\s*(?:put|patch|post|delete)|\\b(?:put|patch|post|delete)\\b)[\\s\\S]*/(?:actions/(?:secrets|variables)|environments/[^\\s/]+/(?:secrets|variables))\\b|/(?:actions/(?:secrets|variables)|environments/[^\\s/]+/(?:secrets|variables))\\b[\\s\\S]*(?:(?:-X|--request|--method(?:=|\\s+))\\s*(?:put|patch|post|delete)|\\b(?:put|patch|post|delete)\\b))"
      action: reject
      message: Direct HTTP writes to GitHub Actions or environment secrets and variables bypass the authorization-authority guardrail. Launchplane auth gaps must escalate to the DB-native authorization architecture.
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

## Agent/Operator Contract

Use `references/agent-operator-contract.json` as the checked-in public contract
for agent/helper operation routing, protected workflow bindings, and semantic
invariants. Run `uv run scripts/check-agent-operator-contract.py` after changing
Launchplane helper routes, workflow guidance, lifecycle guidance, governance
boundaries, or the vendored artifact.

The conformance gate is offline and non-authoritative. It proves that the local
artifact, helper bindings, protected workflows, and durable invariants agree;
it does not contact Launchplane and does not prove that the artifact is current
upstream. Use `check-agent-operator-contract-freshness.py compare` or the
scheduled `Launchplane Contract Freshness` workflow for separate advisory
evidence. Treat matching semantic digests as `current`, a valid mismatch as
`known-stale`, and unavailable or insufficient evidence as `unknown`. Ignore
provenance-only source SHA movement when the semantic digest is unchanged.

`unknown` never grants runtime authority and never opens a drift issue. A
scheduled `known-stale` result opens or updates one maintenance issue through
the maintained GitHub helpers; repeated mismatches reuse the same open issue.
Manual dispatch is compare-only unless issue reporting is explicitly selected.

Keep durable fail-closed rules local: Owner acceptance is authoritative,
engineering review is advisory, GitHub projection is routing/status only,
authorization/admission/landing are independent, protected workflow dispatch
and watching stay delegated to `github_workflow_babysit.py`, and raw protected
workflow dispatch is not allowed. Source projected HTTP paths from the vendored
operation map rather than adding duplicate literals.

The generic-web deploy-recovery dry-run and apply routes are explicit bounded
local extensions because the current upstream 12-operation projection does not
contain them. Do not describe them as contract-backed. If a later artifact adds
those routes, migrate them deliberately and remove the local-extension entries
instead of retaining parallel sources of truth.

### Contract-Backed Lifecycle And Repair Routing

For lifecycle retirement, managed authorization reconciliation, and stable-lane
repair, resolve the requested operation from the vendored contract before
choosing a surface. Use the operation's modes, idempotency, reviewed-evidence
requirements, and supported surfaces as the boundary. When the selected surface
is `protected_workflow`, resolve exactly one protected-workflow binding whose
route matches the operation path, then delegate dispatch and watching to the
`github` skill and `github_workflow_babysit.py`. Never open-code workflow
authentication, dispatch, polling, retry, or reconciliation in this skill.

Fail closed when the contract does not contain one unambiguous operation and,
when required, one unambiguous workflow binding. Report the scenario as an
unsupported capability gap and track focused follow-up coverage; do not route it
through a nearby helper, workflow, or endpoint. Plan or dry-run first, and do not
apply without every contract-required reviewed-evidence field, explicit operator
approval, and an apply-eligible result. Detached application retirement must
preserve zero authority writes and is complete only when candidate absence is
proved.

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

## Stable Deploy Identity

For a Dokploy application target, the product build workflow must publish both
an immutable image digest and an immutable SHA tag in the same repository before
requesting deployment. Send the digest-pinned `repository@sha256:digest` value
as `artifact_id` and the published `repository:sha-<commit>` tag as
`deploy_reference`. Launchplane keeps `artifact_id` as the evidence and runtime
artifact identity, uses `deploy_reference` only for the provider-facing Dokploy
image, and records that provider tag as `image_reference` for read-back evidence.

Treat non-floating tags as an agent-side safety requirement even if a current
validator does not recognize every branch-shaped tag. Never use `latest`, a
branch name, an environment name, or another floating tag as
`deploy_reference`. This two-reference requirement is specific to application
targets; resolve the target category from Launchplane context or the supported
operator surface rather than product workflow inputs or checked-in metadata.

If the SHA tag was not published or the deploy payload omitted
`deploy_reference`, repair the product repository's build/deploy workflow and
publish a new immutable pair. Do not edit Dokploy or Launchplane runtime records
directly. Delegate protected workflow dispatch and watching to the `github`
skill.

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
  Before choosing a next step, classify the denial. A scope denial means an
  existing Launchplane capability does not grant this identity the requested
  scope. A capability gap means no supported Launchplane surface owns the
  operation. Neither case is solved by selecting another credential or CI job.
- **GitHub Is Not Authorization Authority**: A GitHub workflow, Actions secret,
  repository or environment variable, OIDC role, or CI job is never evidence
  that a denied Launchplane action is authorized. Do not author, edit, extend,
  retarget, or dispatch a workflow to carry a call Launchplane denied.
  Repository workflow content is routing metadata, just like
  `.github/github.json`; Launchplane DB/service records remain authority.
- **Unsupported Helper Coverage**: If the helper lacks a command for the needed
  runtime record operation, treat that as a capability gap. Stop at the
  supported Launchplane service/UI path and use `github-plan` to open or update
  the owning authorization-architecture issue, record the denied operation and
  trace ID, and add a native `blocked-by` edge from the affected work. Do not
  synthesize record payloads from issue text, checked-in examples, workflow
  defaults, provider observations, or local files, and do not close the gap with
  a workflow.
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

### Sanctioned Reconciliation And Break-Glass

A Launchplane-owned reconciliation entrypoint may run under GitHub Actions OIDC.
That path executes authority Launchplane already granted; it never creates
authority merely because another identity was denied. Use it only when every
condition holds:

- the entrypoint already exists and is named in repository routing metadata,
  the operator contract, or explicit operator instruction;
- Launchplane owns it and already sanctions it for this exact record type;
- the operator initiated this run for the specific record;
- the workflow, inputs, permissions, target, and secrets are used unmodified;
- dry-run and reviewed evidence precede apply where supported.

Delegate dispatch and watching to the `github` skill. If any condition fails,
stop and escalate architecturally. Bootstrap and break-glass are
operator-initiated exceptions with an audit trail; they are never the routine
answer to `authorization_denied`.

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
- **Post-Merge Checkout Handoff**: After the controller confirms a final landing
  commit, delegate post-merge default-branch freshness to `github` with the exact
  final landing SHA from that terminal controller result and an explicit source
  worktree belonging to the landed repository. Never substitute a PR head,
  candidate, admission, observation, landing-plan, queued, or other intermediate
  SHA, and never use an unrelated controller working directory as the source
  worktree. That handoff uses the landed repo-local reconciler for a runtime-bound
  checkout and safely fast-forwards a unique clean non-runtime default checkout,
  or reports the stale-checkout hint when local state is unsafe. Preserve
  Launchplane landing success independently when local reconciliation is blocked
  or fails, and block only claims that installed runtime behavior or the local
  default checkout is current.
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
  change-impact policy dry-run/apply/read-back, and merge-train controller calls.
- `scripts/check-agent-operator-contract.py`: Hermetic schema, digest,
  public-safety, operation, workflow, invariant, and local-consumer conformance
  gate. A green result is not upstream freshness evidence.
- `operator-config-diagnostic`: Redacted source-presence diagnostic for local
  operator URL and token configuration. Global options such as `--url` must come
  before the subcommand.
- `POST /v1/agent/write-intents/evaluate`: Product-config preflight surface for
  authorization and managed-secret binding evidence; never carries plaintext.
- `POST /v1/product-config/apply`: Primary product-config operator path for
  signed-in/scoped operators; dry-run before apply.
- `POST /v1/change-impact/policies/apply`: Change-impact policy dry-run/apply
  path for explicit private operator input; apply must pin the dry-run policy
  digest and be followed by bounded read-back.
- `GET /v1/change-impact/policy`: Bounded active policy read-back for exact
  revision and digest verification.
- `POST /v1/admin/generic-web/deploy-recovery/dry-run`: Generic-web
  deploy-recovery dry-run path; always run before apply and capture the
  `recovery_digest` from the redacted result.
- `POST /v1/admin/generic-web/deploy-recovery/apply`: Generic-web
  deploy-recovery apply path; requires the original deploy idempotency key,
  the dry-run digest, and reviewed acknowledgement.
- `POST /v1/work-graph/merge-train/controller/run-once`: Preferred merge-train
  controller path; call repeatedly to advance one safe phase at a time.
- `POST /v1/previews/pr-feedback/remediation`: Contract-backed bounded preview
  feedback remediation path; dry-run before apply.
- Launchplane host-only CLI helpers: Use only when you are explicitly on the
  Launchplane host via SSH or the repo provides a concrete command. Do not
  assume a global `launchplane` binary exists on ordinary workstations.
