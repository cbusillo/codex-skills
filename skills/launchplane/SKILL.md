---
name: launchplane
description: Use for Launchplane-managed product/runtime state, secrets, config, deployments, rollout direction, product ownership boundaries, merge-train flow, and audited admin mutations. Use with github-plan when Launchplane work needs to stay aligned with a durable plan, issue graph, blockers, or rollout sequence. If authority is unknown or discovering private infrastructure access, use docs-lookup first.
metadata:
  short-description: Operate Launchplane-managed state
resources:
  - path: scripts/launchplane-owner-review.py
    kind: script
    description: Reads one scoped saved Client decision with its full prose and GitHub delivery receipt.
  - path: scripts/launchplane-context.py
    kind: script
    description: Read Launchplane context for a repository, branch, issue, or pull request.
  - path: scripts/launchplane-write-action.py
    kind: script
    description: Perform bounded Launchplane write-action preflight, dry-run, apply, and merge-train controller calls.
  - path: scripts/check-agent-operator-contract.py
    kind: script
    description: Validate the vendored public agent/admin contract and local consumer bindings offline.
  - path: scripts/check-agent-operator-contract-freshness.py
    kind: script
    description: Compare the vendored contract with the current public upstream artifact and report semantic drift.
  - path: scripts/launchplane_ordinary_agent_client.py
    kind: script
    description: Use a private ordinary-agent credential with durable proof and handle custody.
  - path: scripts/launchplane-ordinary-agent.py
    kind: script
    description: Thin CLI for resuming the private ordinary-agent lifecycle.
  - path: references/ordinary-agent-client.md
    kind: reference
    description: Private client custody, connection, session, retry, and cancellation behavior.
  - path: references/agent-operator-contract.json
    kind: reference
    description: Vendored public Launchplane agent/admin contract artifact.
  - path: references/agent-operator-contract.md
    kind: reference
    description: Contract identity, validation, freshness, and local-extension guidance.
  - path: references/context-helper-contract.md
    kind: reference
    description: Contract for Launchplane context helper configuration, fallback, output, and redaction behavior.
  - path: references/operator-contract.md
    kind: reference
    description: Admin safety contract for Launchplane private config, credentials, and runtime mutations.
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
    description: Public-safe example for private admin helper configuration.
  - path: scripts/launchplane-train-drive.py
    kind: script
    description: Drives one labeled pull request through the merge train to a landed, failed, needs_owner, or error outcome.
commands:
  - name: launchplane-privileged-policy-propose
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "privileged-policy-propose", "--payload-file", "<private-envelope>"]
    purpose: Creates an inert pending policy plan and returns its Director UI review path without approval or apply.
  - name: launchplane-live-target-runtime-sync-dry-run
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "live-target-runtime-sync-dry-run", "--product", "<product>", "--context", "<context>", "--instance", "<instance>"]
    purpose: Plans delivery of existing managed runtime values and reports changed key names only.
  - name: launchplane-live-target-runtime-sync-apply
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "live-target-runtime-sync-apply", "--product", "<product>", "--context", "<context>", "--instance", "<instance>", "--reviewed-dry-run", "--expected-plan-digest", "<digest>", "--dry-run-evidence-file", "<private-file>", "--idempotency-key", "<key>"]
    purpose: Applies a reviewed key plan without deployment and verifies provider env persistence.
  - name: launchplane-owner-review
    source: skill
    resource_path: scripts/launchplane-owner-review.py
    example_argv:
      ["uv", "run", "scripts/launchplane-owner-review.py", "--repo", "OWNER/REPO", "--pr", "42"]
    purpose: Reads private Client feedback and its saved delivery receipt through the configured scoped service route.
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
  - name: launchplane-ordinary-agent
    source: skill
    resource_path: scripts/launchplane-ordinary-agent.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-ordinary-agent.py",
        "--url",
        "<service-url>",
        "session-status",
      ]
    purpose: Resume one private ordinary-agent enrollment, session, or finite job alias with redacted output.
  - name: launchplane-merge-train-policy-read
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "merge-train-policy-read", "--repo", "OWNER/REPO"]
    purpose: Reads active service policy and projects only one repository's enrollment and enqueue routing.
  - name: launchplane-context
    source: skill
    resource_path: scripts/launchplane-context.py
    example_argv:
      ["uv", "run", "scripts/launchplane-context.py", "--repo", "OWNER/REPO"]
    purpose: Reads Launchplane context through the structural helper.
  - name: launchplane-repository-inventory-read
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "repository-inventory-read",
        "--repository-id",
        "<repository-id>",
      ]
    purpose: Reads bounded current repository inventory metadata through the deployed service.
  - name: launchplane-repository-inventory-dry-run
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "repository-inventory-dry-run",
        "--payload-file",
        "<private-file>",
        "--idempotency-key",
        "<stable-key>",
      ]
    purpose: Dry-runs an exact private repository inventory revision and emits review-bound redacted evidence.
  - name: launchplane-repository-inventory-apply
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "repository-inventory-apply",
        "--payload-file",
        "<private-file>",
        "--idempotency-key",
        "<key>",
        "--reviewed-dry-run",
        "--expected-inventory-digest",
        "<dry-run-digest>",
        "--dry-run-evidence-file",
        "<private-dry-run-output>",
      ]
    purpose: Applies only an exact reviewed repository inventory revision with payload, digest, and idempotency binding.
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
  - name: launchplane-product-expected-config-dry-run
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "product-expected-config-dry-run", "--payload-file", "<file>"]
    purpose: Reviews product configuration metadata additions and removals without accepting credential values.
  - name: launchplane-product-expected-config-apply
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "product-expected-config-apply", "--payload-file", "<file>", "--dry-run-evidence-file", "<review-file>", "--reviewed-dry-run", "--idempotency-key", "<key>"]
    purpose: Applies the exact reviewed configuration metadata through the service's product-scoped authority.
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
    purpose: Reports redacted admin URL/token source presence before write-capable helper calls.
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
  - name: launchplane-target-replacement-operation-read
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "target-replacement-operation-read",
        "--operation-id",
        "<operation-id>",
      ]
    purpose: Reads one Odoo deploy operation's status, phase, times, error code, bounded description and env-key names.
  - name: launchplane-target-replacement-plan-read
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "target-replacement-plan-read",
        "--product",
        "<product>",
        "--instance",
        "<instance>",
      ]
    purpose: Reads an Odoo lane's replacement plan status, blocker codes and env-key names.
  - name: launchplane-merge-train-policy-import-dry-run
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "merge-train-policy-import-dry-run",
        "--payload-file",
        "<private-file>",
        "--expected-current-policy-digest",
        "<active-policy-digest>",
      ]
    purpose: Dry-runs an explicit private merge-train policy import after active-policy digest preflight.
  - name: launchplane-merge-train-policy-import-apply
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "merge-train-policy-import-apply",
        "--payload-file",
        "<private-file>",
        "--expected-current-policy-digest",
        "<active-policy-digest>",
        "--expected-new-policy-digest",
        "<candidate-policy-digest>",
        "--reviewed-dry-run",
        "--dry-run-evidence-file",
        "<private-dry-run-output>",
        "--idempotency-key",
        "<key>",
      ]
    purpose: Applies only reviewed merge-train policy import evidence after active-policy digest preflight.
  - name: launchplane-generic-web-deploy-recovery-reference-read
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "generic-web-deploy-recovery-reference-read", "--product", "<product>"]
    purpose: Reads a service-owned reference to one exact held testing event deploy without collecting its request or key.
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
    - id: prefer-launchplane-write-helper-for-merge-train-policy-import-api
      match:
        shell_regex: "\\b(curl|wget|http)\\b.*\\b/v1/merge-train/policies/import\\b"
      action: require_preferred
      message: Raw Launchplane merge-train policy imports bypass helper-owned private-file, active-digest preflight, reviewed evidence, idempotency, redaction, and ambiguity handling. Use the write-action helper.
      preferred:
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-write-action.py",
              "merge-train-policy-import-dry-run",
              "--payload-file",
              "<private-file>",
              "--expected-current-policy-digest",
              "<active-policy-digest>",
            ]
          purpose: Dry-runs explicit admin policy input after active-policy digest preflight.
    - id: prefer-launchplane-helper-for-repository-inventory-api
      match:
        shell_regex: "\\b(curl|wget|http)\\b.*\\b/v1/repository-inventory(?:/apply)?\\b"
      action: require_preferred
      message: Raw Launchplane repository-inventory calls bypass helper-owned private-file, reviewed evidence, idempotency, redaction, and trace discipline. Use the write-action helper.
      preferred:
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-write-action.py",
              "repository-inventory-read",
              "--repository-id",
              "<repository-id>",
            ]
          purpose: Reads bounded current repository inventory metadata through the deployed service.
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-write-action.py",
              "repository-inventory-dry-run",
              "--payload-file",
              "<private-file>",
              "--idempotency-key",
              "<stable-key>",
            ]
          purpose: Dry-runs exact private inventory input through the bounded helper.
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/launchplane-write-action.py",
              "repository-inventory-apply",
              "--payload-file",
              "<private-file>",
              "--idempotency-key",
              "<key>",
              "--reviewed-dry-run",
              "--expected-inventory-digest",
              "<dry-run-digest>",
              "--dry-run-evidence-file",
              "<private-dry-run-output>",
            ]
          purpose: Applies only the exact reviewed inventory payload through the bounded helper.
    - id: prefer-launchplane-write-helper-for-generic-web-deploy-recovery-api
      match:
        shell_regex: "\\b(curl|wget|http)\\b.*\\b/v1/admin/generic-web/deploy-recovery/(dry-run|apply|[^/\\s?]+/testing)\\b"
      action: require_preferred
      message: Raw Launchplane generic-web deploy-recovery calls bypass helper-owned private-file, dry-run/apply, idempotency, digest binding, redaction, and trace discipline. Use the write-action helper.
      preferred:
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "generic-web-deploy-recovery-reference-read", "--product", "<product>"]
          purpose: Reads the exact held testing event reference through the bounded helper.
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
          purpose: Dry-runs explicit admin recovery input through the bounded helper.
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
    - id: prefer-launchplane-write-helper-for-product-expected-config-api
      match:
        shell_regex: "\\b(curl|wget|http)\\b.*\\b/v1/product-profiles/expected-config/apply\\b"
      action: require_preferred
      message: Use the expected-config helper for private metadata input, bound dry-run review, idempotency, and redacted output.
      preferred:
        - kind: script
          path: scripts/launchplane-write-action.py
          example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "product-expected-config-dry-run", "--payload-file", "<private-file>"]
          purpose: Reviews product configuration metadata additions and removals before any apply.
    - id: prefer-launchplane-write-helper-for-product-config-api
      match:
        shell_regex: "\\b(curl|wget|http)\\b.*\\b/v1/(product-config/apply|agent/write-intents/evaluate)\\b"
      action: require_preferred
      message: Raw Launchplane product-config API calls bypass helper-owned dry-run/apply discipline, redaction, private config sourcing, and traceable admin output. Use the write-action helper.
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
      message: Raw GitHub Actions secret or variable writes can silently make GitHub an authorization authority. Use the owning product/admin contract; Launchplane auth gaps must escalate to the DB-native authorization architecture.
    - id: reject-http-github-actions-secret-writes
      match:
        shell_regex: "(?i)\\b(?:curl|wget|http)\\b[\\s\\S]*(?:(?:(?:-X|--request|--method(?:=|\\s+))\\s*(?:put|patch|post|delete)|\\b(?:put|patch|post|delete)\\b)[\\s\\S]*/(?:actions/(?:secrets|variables)|environments/[^\\s/]+/(?:secrets|variables))\\b|/(?:actions/(?:secrets|variables)|environments/[^\\s/]+/(?:secrets|variables))\\b[\\s\\S]*(?:(?:-X|--request|--method(?:=|\\s+))\\s*(?:put|patch|post|delete)|\\b(?:put|patch|post|delete)\\b))"
      action: reject
      message: Direct HTTP writes to GitHub Actions or environment secrets and variables bypass the authorization-authority guardrail. Launchplane auth gaps must escalate to the DB-native authorization architecture.
    - id: prefer-launchplane-helpers-over-global-cli
      match:
        argv_prefix: ["launchplane"]
      exceptions:
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "service", "export-openapi"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "service", "export-agent-contract"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "service", "export-owner-control-contract"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "ci", "unittest-shard", "local"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "ci", "unittest-shard", "plan"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "ci", "unittest-shard", "run"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "service", "audit-config-authority"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "odoo-ownership", "check"]
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
          purpose: Uses bounded Launchplane mutation entrypoints when an admin action is approved.
---

# Launchplane Expert

The source-only `service export-openapi`, `service export-agent-contract`,
`service export-owner-control-contract`, `service audit-config-authority`,
`odoo-ownership check`, and `ci unittest-shard local|plan|run` gates may run
through `uv run [--extra dev] launchplane` in a verified `cbusillo/launchplane`
checkout or linked worktree, including frontend contract generation. Use the
tool's working directory or a single literal absolute `cd <checkout> &&` prefix.
Other shell directory/project overrides retain the block. PostgreSQL integration
commands that target a database and all live/admin CLI commands retain their
helper route below. Consumers without
repository-exception support keep the original block; do not bypass it through
an alternate entry point.

Use this skill to inspect product/runtime state and perform safe,
authenticated mutations via the Launchplane service API.

Use `docs-lookup` first when a task is discovering the source of truth or access
path for external/private infrastructure and it is not already clear that
Launchplane manages that resource.

## Runtime Authority Boundary

Checked-in files are not runtime authority for Launchplane-managed state. Code
may own schemas, validators, generic behavior, helper routing, fake examples,
and fail-closed defaults. Launchplane service records or explicit scoped
admin input own real product, tenant, repository, branch, domain, lane,
provider-target, runtime-environment, authz, admin identity, route, health-check, and
other mutable runtime values.

This applies even when values are not secrets. Non-secret topology can still
steer production behavior. Treat repo metadata, workflow variables, checked-in
examples, and archived workstation files as hints for which Launchplane helper,
service record, or admin surface to use; never use them as evidence of the
current live value. If the needed live value is only visible in checked-in or
workstation files, stop and obtain Launchplane context or explicit admin
input instead of inferring it.

Favor service-backed audit trails over local ad hoc fallbacks. Use the deployed
service/API or admin UI first for current product state; direct database
access requires an explicitly approved host-side context. Archived files under
`~/.config/launchplane/`, including `service.env`, `dokploy.env`, and
`runtime-environments.toml`, are historical clues only.

When a repo has `.github/github.json`, inspect its `launchplane` block before
looking in sibling repos, archived workstation files, or workflow variables. The
repo block is public-safe routing metadata only: it may name helper paths,
environment variable names for service URLs, local config examples,
and GitHub Actions workflow entrypoints. Merge-train enrollment, base branches
and enqueue labels come only from the active service policy read; point `docs`
at the [train procedure](references/merge-train.md) rather than storing a
`launchplane.mergeTrain` block. It must not
contain tokens, secret values, cookies, concrete Launchplane service URLs,
private credential paths, provider payloads, product/runtime endpoints, or
plaintext runtime configuration. Treat Launchplane-managed product, app,
preview, deploy, provider, lane, tenant, and health-check coordinates as service
records, not checked-in repo metadata; if repo metadata and Launchplane service
state disagree, service/admin state wins and the metadata is stale routing
context to fix deliberately.

## Agent/Admin Contract

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

Keep durable fail-closed rules local: Client acceptance is authoritative (see
the [release rule](../references/role-words.md#release-rule)),
engineering review is advisory, GitHub projection is routing/status only,
authorization/admission/landing are independent, protected workflow dispatch
and watching stay delegated to `github_workflow_babysit.py`, and raw protected
workflow dispatch is not allowed. Source projected HTTP paths from the vendored
operation map rather than adding duplicate literals.

The privileged-policy-propose, merge-train policy read and import, repository inventory, product expected configuration,
generic-web deploy-recovery, live-target-runtime sync, Odoo addon-settings, integration-allowances,
testing-hold, product-repository-identity, product-environment-read,
product-activity-read, protected-artifacts-read, product-profile-read, path-check,
preview-history-read,
reconcile-requests-read, product-secret-bindings-read,
target-replacement-operation-read, target-replacement-plan-read, `product-owner-*`,
`product-image-repository-*`, `dokploy-target-create-compose-*`,
`dokploy-target-complete-compose-source-*`, `dokploy-target-reconcile-compose-domain-*`,
`dokploy-target-prune-compose-domain-*`, `production-backup-authority-*`,
`private-health-endpoint-*`, `health-monitoring-*`, product-promotion-status-read and
product-promotion-dry-run commands are explicit bounded local
extensions because the vendored public
operation projection does not contain their routes. Do not describe them as contract-backed. If a later artifact adds those
routes, migrate them deliberately and remove the local-extension entries instead
of retaining parallel sources of truth.

### Agent Policy Proposals

For a prepared managed access-policy or merge-train policy proposal, read
[the proposal helper contract](references/write-action-helper-contract.md#policy-proposals)
and use `launchplane-write-action.py privileged-policy-propose --payload-file
<private-envelope>`. The deployed service requires an explicit managed proposer
grant for its configured `local_operator` identity. The Director approves that grant
separately through the Access policy card. Submit no proposal until the service
supports this route and the grant is installed. Source delivery grants no access.

Creation is pending and inert. Return the review path to the Director; approval
stays in the signed-in UI and the proposer can never approve or apply. Reuse the
private envelope's stable source event when a response is uncertain. This path
uses private `local_operator` config, independent of terminal-agent and ordinary-agent
credentials, enrollment, sessions, or leases.

### Private Ordinary-Agent Client

For an ordinary agent connection, session, finite job, or cancellation, read
[the private client contract](references/ordinary-agent-client.md) and use
`scripts/launchplane-ordinary-agent.py`. Keep credential claims inside that
adapter; its public output must never contain the claim response or receiver
proof. Present the service's review link for a pending administrator decision.
Resume saved requests after interruption, and reuse issued session and lease
handles without asking the user to locate or type them. A proposal does not
approve itself, and client installation does not activate a worker.

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
apply without every contract-required reviewed-evidence field, explicit admin
approval, and an apply-eligible result. Detached application retirement must
preserve zero authority writes and is complete only when candidate absence is
proved.

## Rollout Plan Alignment

For Launchplane rollout, runtime, product-boundary, merge-train, or admin
work, do not continue from the latest operational finding alone. Before the next
slice, state how it fits the active Launchplane plan, issue graph, rollout
sequence, or product ownership boundary.

If an operational finding changes the plan, update the owning GitHub plan issue
or PR before treating the new path as canonical. Prefer explicit blocker,
sub-issue, or related-issue edges over burying direction changes in chat.

When Launchplane work turns into GitHub issue, PR, Actions, review, comment,
commit, or push work, delegate that surface to `github` or `github-plan` before
running commands. Launchplane owns runtime/admin authority; the GitHub skills
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

### Client feedback

Use `uv run scripts/launchplane-owner-review.py --repo OWNER/REPO --pr NUMBER`
to retrieve the complete saved Client reason; add `--decision-id ID` for a
historical decision. This narrow read uses the existing private admin config
and the product's `product_profile.read` permission. It is a bounded local
extension of `/v1/product-review`, separate from the public-safe context output.
The output contains Client prose: keep it in task-private evidence unless its
publication is authorized. A failed read means unavailable, not no feedback.
The PR watcher uses this reader to verify its GitHub projection and receipt.

## Stable Deploy Identity

Before preparing or repairing an application-target deployment, read
[stable deploy identity](references/deploy-identity.md). Publish both an
immutable digest and immutable SHA tag before deployment; never use a floating
tag. Resolve the target category from Launchplane context or the admin
surface. Repair missing references through the product repository's build/deploy
workflow and delegate protected workflow dispatch and watching to `github`.

## Runtime Management (Admin)

Mutate runtime environments, managed secrets, and product config.

- **Safety**: Strictly follow the `references/operator-contract.md`.
- **Helper Contract**: Use `references/write-action-helper-contract.md` for
  bounded helper entrypoints, exit behavior, and redacted output shape.
- **Auth**: Prefer signed-in, scoped admin sessions in the Launchplane UI or
  service API. Source terminal/local admin credentials only through the
  admin contract; do not paste token values into chat, issues, PRs, docs, or
  logs.
- **Private Config**: For non-browser terminal execution, use the source order
  in the admin contract. Missing private config means the write-capable path
  is unavailable and must fail closed; do not use `.github/github.override.json`
  for Launchplane credentials.
- **Admin Diagnostics**: Before concluding admin access is unavailable,
  run `scripts/launchplane-write-action.py operator-config-diagnostic`. Treat
  `launchplane-context` availability and local admin readiness as separate
  checks: context can be unavailable while the write helper is usable, and the
  write helper can be blocked only by missing local admin config. If the
  diagnostic reports `missing_service_url`, token material was found but no
  write-capable Launchplane service URL source was found; configure
  `LAUNCHPLANE_OPERATOR_URL` in the private local admin env file or pass
  `--url` before the subcommand, then rerun the diagnostic. If the active shell
  has a service URL under `LAUNCHPLANE_PUBLIC_URL` but not
  `LAUNCHPLANE_OPERATOR_URL`, treat it as an ambiguous URL source: obtain the
  correct admin URL and pass it with `--url` before the subcommand, or copy
  the sanctioned value into private admin config. Do not use public URL
  variables as write authority.
- **Runtime Sources**: Apply the Runtime Authority Boundary above to reads and
  writes: never add or copy product authz grants, target IDs, tenant domains, seed/import
  payloads, route batches, or live topology into deploy scripts, workflow defaults,
  or repo config or product repos. Committed examples use fake placeholders or intentionally public,
  non-authoritative sample data. Keep concrete service URLs and credentials in
  private admin config, environment variables, GitHub Actions OIDC, or signed-in
  Launchplane UI sessions. For shared/prod, use the deployed service, admin UI,
  or bounded helper/API with the correct URL and scoped credentials.
- **Runtime sync**: For delivery of existing managed values to a lane's provider
  env, read [runtime sync](references/write-action-helper-contract.md#managed-runtime-sync)
  and use `live-target-runtime-sync-dry-run` / `-apply`. Sync does not restart
  containers. A denial stays with that identity; it does not authorize recovery,
  deployment or another credential.
- **First Shot**: For product-config/runtime/secret sync, use the service API
  path from the admin contract first. Do not start by searching for a local
  `launchplane` binary or by poking provider config directly.
- **Denied Actions**: A local admin token can be present and still lack a
  specific action. Report that as authorization denial, not missing credential.
  Before choosing a next step, classify the denial. A scope denial means an
  existing Launchplane capability does not grant this identity the requested
  scope. A capability gap means no supported Launchplane surface owns the
  operation. Neither case is solved by selecting another credential or CI job.
  Block only the affected operation or work item, preserve the trace and source
  URL, and continue independent safe work when available. Stop the whole
  session only when no useful unblocked work remains or the failed operation may
  have produced an uncertain effect.
- **GitHub Is Not Authorization Authority**: A GitHub workflow, Actions secret,
  repository or environment variable, OIDC role, or CI job is never evidence
  that a denied Launchplane action is authorized. Do not author, edit, extend,
  retarget, or dispatch a workflow to carry a call Launchplane denied.
  Repository workflow content is routing metadata, just like
  `.github/github.json`; Launchplane DB/service records remain authority.
- **Unsupported Helper Coverage**: If the helper lacks a command for the needed
  runtime record operation, treat that as a capability gap. Stop the affected
  operation at the supported Launchplane service/UI path and use `github-plan`
  to open or update the owning authorization-architecture issue, record the
  denied operation and trace ID, and add a native `blocked-by` edge from the
  affected work. Re-rank or continue unrelated work instead of turning one gap
  into a session-wide stop. Do not synthesize record payloads from issue text,
  checked-in examples, workflow defaults, provider observations, or local
  files, and do not close the gap with a workflow.
- **Workflow**:
  1. Inspect Context to identify the target and change needed. Before asking
     the Director to approve a dry run, apply, recovery, or onboarding, check
     the whole path read-only as [task scope](../references/execution-scope.md)
     describes, including earlier refusals of the same operation in run
     history, and ask once with every blocker found.
  2. Run admin config diagnostics before a write-capable helper call when
     target URL, token source, or authority is unclear.
  3. If diagnostics report `missing_service_url`, fix local admin routing
     first. This is a workstation setup problem, not PR readiness, merge-train
     admission, or scheduler state.
  4. Preflight product-config intent with `scripts/launchplane-write-action.py
product-config-preflight` when agent-side authorization or managed-secret
     binding evidence is useful.
  5. Use the signed-in, scoped admin path when a human-approved runtime or
     managed-secret mutation is required.
  6. Build a product-config request for `POST /v1/product-config/apply` only in
     an approved admin surface. The helper may submit dry-run/apply from a
     private local payload file, never from chat, CLI plaintext secret args, or
     committed examples.
  7. **Dry-run** and inspect redacted results.
  8. **Apply** with a concrete reason only after the dry-run succeeds and the
     admin's intent is explicit.
  9. Inspect returned `next_actions` and complete required follow-up actions;
     product-config apply can update Launchplane records before the live target
     runtime has been synced.

Agents may guide the admin, prepare request shape, summarize redacted dry-run
evidence, and report trace IDs/status. Agents must not collect plaintext secret
values in chat, issues, PRs, docs, logs, or helper output, and must not bypass
Launchplane by editing provider configuration directly.

### Blocked Work Continuation

Until Launchplane exports a native deferred-operation lifecycle, represent an
authorization or capability gap through the owning GitHub plan relationship:

1. preserve the denied operation, bounded reason, trace ID, and source URL;
2. block only the affected work item on the owning architecture issue;
3. use `github-plan next` or the existing work source to select independent
   work;
4. notify the Director immediately only when every useful item is blocked, an
   effect is uncertain, or an active environment is unhealthy.

Do not tell the Director to provision a new credential or click through a
GitHub Actions workaround. A future Launchplane deferred-operation record may
replace this GitHub-plan handoff after it is present in the public contract.

### Sanctioned Reconciliation And Break-Glass

A Launchplane-owned reconciliation entrypoint may run under GitHub Actions OIDC.
That path executes authority Launchplane already granted; it never creates
authority merely because another identity was denied. Use it only when every
condition holds:

- the entrypoint already exists and is named in repository routing metadata,
  the admin contract, or explicit admin instruction;
- Launchplane owns it and already sanctions it for this exact record type;
- an admin initiated this run for the specific record;
- the workflow, inputs, permissions, target, and secrets are used unmodified;
- dry-run and reviewed evidence precede apply where supported.

Delegate dispatch and watching to the `github` skill. If any condition fails,
stop and escalate architecturally. Bootstrap and break-glass are
admin-initiated exceptions with an audit trail; they are never the routine
answer to `authorization_denied`.

## Merge Train (Controller)

Read enrollment with `uv run scripts/launchplane-write-action.py
merge-train-policy-read --repo OWNER/REPO`. Launchplane's active policy owns
whether a repository is enrolled, its base branches and enqueue labels;
`.github/github.json` only carries routing hints. The bounded read uses the
existing private admin configuration and standing `merge_train.policy_targets`
permission. It emits only that repository's targets and policy revision evidence.
If configuration, access or a valid service response is unavailable, enrollment
is unknown. Never infer a direct merge path from missing metadata or a failed read.

Use Launchplane's controller route as the default merge-train workflow. Before
advancing or diagnosing a train, read [merge-train execution](references/merge-train.md)
for phase, stack, retry, and terminal evidence requirements. Label every PR
in a stack that is ready to land, and never hand-collapse stacks.

- **Preferred Route**: `POST /v1/work-graph/merge-train/controller/run-once`.
- **Helper**: Use `scripts/launchplane-write-action.py
merge-train-controller-run-once` instead of open-coding the route. Mutating
  calls require an idempotency key.
- **Driving To An Outcome**: To take a labeled PR through its train, run
  `uv run scripts/launchplane-train-drive.py --repo OWNER/REPO --pr N` in the
  background instead of writing a loop around the run-once helper. It pauses on
  every non-terminal state, stops at a wall-clock deadline, reports every PR in
  the landing batch, and exits `landed` (0), `failed` (1), `needs_owner` (2) or
  `error` (3) with a JSONL `stop` event in `gh_pr_watch.py`'s shape. The
  controller refreshes a behind-base PR itself when it can; pass
  `--allow-branch-update` only for your own same-repository branches, for a
  behind-base PR it reports without refreshing. Run one driver per repository
  train, and keep a watcher on it while you report "waiting on the train".
  When the landed repository is the skills catalog the driver lives in, its
  landed `stop` event carries `runtime_reconciliation`, the runtime
  reconciler's receipt for that landing; report it as the runtime outcome.
- **Train Entry**: Read the target branch's enqueue label from the active
  policy and put that label on the root PR and every stacked child ready to land
  with it. Preserve a task's assigned Supervisor handoff instead of applying
  labels when the task does not authorize train entry. A held child stays unlabeled or draft, and that stops the stack. Do not
  hand-collapse stacks in GitHub.
- **Mutation Gate**: Keep scheduled runners in dry-run mode until the Director
  explicitly selects a mutation pilot. Manual `mutate=true` controller runs are
  appropriate only after dry-run evidence shows the intended queue, candidate,
  and next action. Do not leave scheduled mutation enabled as a casual default.
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
- **Boundaries**: Merge-train behavior is DB/policy-backed. Do not hardcode
  repositories, labels, tokens, protected branches, or local file config.

## Intentionality & Safety

This skill combines inspection and mutation. You must explicitly announce when
you are transitioning from **Inspecting Context** to **Executing Admin
Actions**. Never apply a mutation without a preceding dry-run and situational
verification.

## Tools

- `scripts/launchplane-context.py`: Structural state helper.
- `scripts/launchplane-write-action.py`: Public-safe write-action wrapper for
  product-config intent preflight, private local product-config dry-run/apply,
  guarded merge-train policy
  import, repository inventory read/dry-run/apply, product environment,
  activity, protected-artifact, preview, reconcile and secret-binding metadata reads, Odoo
  target-replacement operation and plan reads, Client, image repository,
  Dokploy compose target, production backup authority and private health
  endpoint dry-run/apply with read-back, product promotion status and dry-run, and
  merge-train controller calls.
- `scripts/check-agent-operator-contract.py`: Hermetic schema, digest,
  public-safety, operation, workflow, invariant, and local-consumer conformance
  gate. A green result is not upstream freshness evidence.
- `operator-config-diagnostic`: Redacted source-presence diagnostic for local
  admin URL and token configuration. Global options such as `--url` must come
  before the subcommand.
- `POST /v1/agent/write-intents/evaluate`: Product-config preflight surface for
  authorization and managed-secret binding evidence; never carries plaintext.
- `POST /v1/product-config/apply`: Primary product-config admin path for
  signed-in, scoped admins; dry-run before apply.
- `POST /v1/merge-train/policies/import`: Bounded local-extension path for
  private merge-train policy dry-run/apply. Require active-policy digest
  preflight, exact saved dry-run evidence, reviewed acknowledgement,
  idempotency, and post-apply read-back. The helper-side digest check is not
  server-enforced compare-and-swap.
- `GET /v1/work-graph/merge-train/policy`: Bounded local-extension read
  (`merge-train-policy-read --repo OWNER/REPO`) of enrollment and enqueue routing
  from the complete active policy. Credential sources and other repositories
  are dropped. A confirmed complete read with no match means not enrolled; any
  failure leaves enrollment unknown. Reading does not authorize train entry.
- `GET /v1/work-graph/merge-train/policy-targets`: Internal bounded preflight
  read used immediately before policy import to verify the expected active
  policy digest. It is not an independently exposed helper command.
- `GET /v1/repository-inventory`: Bounded current repository inventory read for
  exact immutable GitHub repository identity.
- `POST /v1/repository-inventory/apply`: Repository inventory dry-run/apply
  route. Use private payload files; apply requires reviewed helper evidence,
  inventory digest binding, and a stable idempotency key.
- `POST /v1/admin/generic-web/deploy-recovery/dry-run`: Generic-web
  deploy-recovery dry-run path; always run before apply and capture the
  `recovery_digest` from the redacted result. Before event-driven testing
  recovery, read [the helper recovery contract](references/write-action-helper-contract.md#generic-web-deploy-recovery)
  and obtain `generic-web-deploy-recovery-reference-read --product P`. Use the
  returned reference instead of reconstructing a deploy request or key.
- `POST /v1/admin/generic-web/deploy-recovery/apply`: Generic-web
  deploy-recovery apply path; requires the dry-run digest and reviewed
  acknowledgement. Original-deploy payloads require the original key; reference
  payloads resolve it inside Launchplane.
- `POST /v1/product-config/odoo-addon-settings/apply`: Projected contract
  path for an Odoo lane's Shopify addon settings on its instance-override record
  (`odoo-addon-settings-dry-run` / `odoo-addon-settings-apply`). The private
  payload carries the store key, API version and `test_store` as literals and
  the token and webhook key as managed secret binding ids only. Apply requires
  the saved dry-run evidence, `--expected-plan-digest`, reviewed
  acknowledgement and an idempotency key. The record change is intent only: run
  Odoo post-deploy for the lane afterwards.
- `GET /v1/product-config/integration-allowances` and
  `POST /v1/product-config/integration-allowances/apply`: Bounded
  local-extension paths for a lane's non-production integration allowances
  (`integration-allowances-read` / `-dry-run` / `-apply`). The private payload
  carries the lane's whole allowance list, each with `integration`, `kind`
  (`dev_store`, `read_only_source` with grant `evidence`, or `pre_live`) and
  `reason`. Apply requires the saved dry-run evidence, `--expected-plan-digest`,
  reviewed acknowledgement and an idempotency key. Use the product's canonical id
  (for example `odoo-tenant-opw`); the service checks that it owns the lane.
- `GET /v1/product-config/testing-hold` and
  `POST /v1/product-config/testing-hold/apply`: Bounded local-extension paths
  for a testing lane's staff-testing hold (`testing-hold-read` / `-dry-run` /
  `-apply`, with `--hold` or `--lift` and `--reason`). Apply requires the same
  saved-dry-run safeguards; lifting requests a testing reconcile.
- `POST /v1/product-profiles/repository-identity/apply`: Bounded local-extension
  path that records a product's repository id from tracked inventory
  (`product-repository-identity-dry-run` / `-apply`, with `--product` and
  `--reason`). Apply requires the same saved-dry-run safeguards.
- `GET /v1/products/{product}/environments/{environment}` and
  `GET /v1/products/{product}/activity`: Bounded local-extension reads
  (`product-environment-read --product --environment` and
  `product-activity-read --product`) for confirming what a lane actually runs:
  the current artifact id, source commit, image digest, expected and observed
  runtime identity with its deployment record id, health status, and recent
  deployment, promotion and backup-gate events with their record ids. Settings,
  secrets, actions, URLs and provider target names are dropped.
- `GET /v1/artifacts/protected`: Bounded local-extension read
  (`protected-artifacts-read --product P [--context C]`) of protected artifacts:
  per-entry reason, context, instance, artifact id, source record type and id,
  and image digest. It returns sanitized warning texts, total entry and warning
  counts, and truncation flags. Image reference lists, URLs and other provider fields are
  dropped. Identifier validation and retention limits follow the
  [helper contract](references/write-action-helper-contract.md#product-environment-activity-and-preview-reads).
- `GET /v1/product-profiles/{product}`: Bounded local-extension read
  (`product-profile-read --product`) for who a product's Client is and its
  `production_use`: `prelaunch` skips Client release review, while `live` and
  `unknown` require it. It also returns the display name, driver, repository,
  lifecycle state and lane contexts and instances. Images, URLs, workflows and
  expected configuration are dropped.
- `GET /v1/products/{product}/path-check`: Bounded local-extension read
  (`path-check --product P --path testing|promote`). Use it first when asking
  whether this identity can take this product to done. It answers for the
  caller's own identity and requires `product_environment.read` on the
  product's lane contexts. It returns every ordered step as `clear`, `blocked`
  or `unknown`, with counts, a code, Launchplane's fixed description, fix kind
  (`none` for clear steps), and evidence record ids. Unknown fields and unsafe
  names or values fail closed. The read writes nothing and grants no authority;
  a clear path does not replace approval or the action's own gates.
- `GET /v1/previews/{preview_id}/history`: Bounded local-extension read
  (`preview-history-read`, by `--preview-id` or by `--context`, `--repository`
  and `--pr`) for confirming what a preview serves: its state, serving
  generation, and each generation's artifact, PR head SHA, image digest and
  failure stage.
- `GET /v1/product-profiles/{product}/reconcile-requests`: Bounded
  local-extension read (`reconcile-requests-read --product`) for what the
  event reconciler last decided for each of a product's previews and its
  testing lane: state, attempt, delivery id, last error, and the plan's
  action, reason, commit, digests and ids. When testing keeps an older build,
  `rejected_builds` lists the newer commits that failed verification. Use `--target-key <exact-target-key>`
  to select a target from the service response before the helper's output bound;
  a truncated unselected read cannot prove that testing is absent. An empty
  selection describes only the returned service list, not records outside it.
- `GET /v1/products/{product}/secret-bindings`: Bounded local-extension read
  (`product-secret-bindings-read --product`) of a product's runtime secret
  binding metadata: binding key, name, scope, context, instance, declared
  class, sharing reason and current `version_id`. It never returns a value or
  ciphertext, and lists only bindings the caller's `secret.list` access covers,
  so an empty list does not prove absence. Use a binding's context, instance
  and `version_id` as `copy_from` in a product-config secret entry to copy a
  product's own stable-lane secret into another of its lanes without
  collecting the value; see the
  [write-action contract](references/write-action-helper-contract.md#copying-a-managed-runtime-secret).
- `GET /v1/drivers/odoo/target-replacement/operations/{operation_id}`: Bounded
  local-extension read (`target-replacement-operation-read --operation-id`)
  for why a testing or stable Odoo deploy failed: take the id from
  `reconcile-requests-read` (`queued_operation_id`, `active_operation_id`,
  `deployed_operation_id` or `last_failed_operation_id`). It returns status,
  phase, times, attempt, and, when present in the response, artifact id,
  image digest and step statuses. Failure details include the error code, Launchplane's fixed `error_description` as a bounded public summary,
  and validated `error_detail_keys` (env-key names only); free-text error
  messages are dropped. The service authorizes it as `operations.read` on
  product `launchplane` for the operation's context and instance, or
  `odoo_target_replacement_apply.execute` on the operation's own product,
  context and instance.
- `POST /v1/drivers/odoo/target-replacement-plan`: Bounded local-extension
  read (`target-replacement-plan-read --product --instance`) for what an Odoo
  target replacement on that lane would do, before any apply. The route builds
  a plan and writes nothing. It returns plan status, strategy, data source
  mode, the expected artifact, blocker codes, and env-key names only: the keys
  it would deliver (`delivered_runtime_keys`, `null` when the service does not
  report them yet) and retire, the keys each blocker is about, and the current
  target's env keys and missing volume keys, plus step ids and statuses.
  Blocker, step and warning text, domains, target names and ids, and volume
  values are dropped and listed by path. The service authorizes it as
  `odoo_target_replacement_plan.read` on the lane.
- `POST /v1/product-profiles/{product}/owner`: Bounded local-extension path
  that records or clears a product's Client (`product-owner-dry-run` /
  `-apply`, with `--product`, `--github-login` or `--clear`, and `--reason`).
  Apply checks the Client has not changed since the review, then reads the
  profile back.
- `POST /v1/product-profiles/{product}/image-repository`: Bounded
  local-extension path that moves a product's image repository to the GHCR
  package named after its repository (`product-image-repository-dry-run` /
  `-apply`, with `--product`, `--image-repository` and `--reason`). The dry run
  shows the repository before and after and each lane's current artifact.
  Apply sends the reviewed starting repository and stops when the profile has
  moved since the review, then reads the profile back.
- `POST /v1/dokploy-targets/setup`: Bounded local-extension path that creates a
  lane's Dokploy compose target from a private payload
  (`dokploy-target-create-compose-dry-run` / `-apply`). Provider ids, server
  ids, domains and git URLs never reach the output; apply reads the target back.
  An explicit `custom_git_branch` and `compose_path` set the product-derived
  repository source on creation. `dokploy-target-complete-compose-source-dry-run`
  / `-apply` complete an empty tracked testing compose with branch/path only,
  saved review evidence, binding/source verification and no deployment. Read
  [Provider target](references/write-action-helper-contract.md#provider-target)
  before either source operation, including partial-outcome recovery.
  Add the lane record afterwards with the contract-backed stable-lane repair
  workflow.
- `GET /v1/production-backup-authority` and
  `POST /v1/production-backup-authority/apply`: Bounded local-extension paths
  for a production lane's backup policy and targets
  (`production-backup-authority-read` / `-dry-run` / `-apply`). The private
  payload carries the Proxmox coordinates; output shows only record ids,
  revisions, kinds and the `authority_digest` apply is bound to.
- `POST /v1/product-profiles/health-monitoring/apply`: Bounded local-extension
  path for one exact lane's health check (`health-monitoring-dry-run` / `-apply`).
  Supply `--product`, `--context`, `--instance`, `--check-name`, `--check-kind`
  (`public_http` or `private_http`), `--monitoring-intent` (`public`, `private`,
  or `prelaunch`), `--enabled` / `--no-enabled`, `--require-runtime-identity` /
  `--no-require-runtime-identity`, and `--reason`. A private check may name an
  existing `--private-endpoint-key`; endpoint URLs and topology fields are never
  accepted. Apply requires saved dry-run evidence, `--expected-plan-digest`,
  `--reviewed-dry-run` and an idempotency key. It checks the endpoint before
  sending, then reads back the exact check and lane intent with private endpoint
  comparison. Receipts omit URLs and free-text reasons. Policy verification
  does not prove monitor observations or sustained cadence. Read the
  [helper contract](references/write-action-helper-contract.md#health-monitoring)
  before using it.
- `GET /v1/private-health-endpoints/records` and
  `POST /v1/private-health-endpoints/apply`: Bounded local-extension paths for
  the private health endpoint record a lane's `private_http` health check names
  (`private-health-endpoint-read --product --context [--instance]` /
  `-dry-run` / `-apply`, with `--payload-file` and `--reason`). The private
  payload carries the endpoint key, scope and URL; output shows keys, scope and
  status only. Apply requires the saved dry-run evidence,
  `--expected-plan-digest`, reviewed acknowledgement and an idempotency key,
  then reads the record back and compares its URL privately.
- `GET .../environments/prod/promotion-status` and
  `POST .../environments/prod/promotion/dry-run` under `/v1/products/{product}`:
  Bounded local-extension reads and dry-runs (`product-promotion-status-read`,
  `product-promotion-dry-run`) for whether a testing-to-prod promotion could
  run. The dry-run takes no backup and deploys nothing. No helper command sends
  a live promotion.
- `POST /v1/work-graph/merge-train/controller/run-once`: Preferred merge-train
  controller path; call repeatedly to advance one safe phase at a time.
- `POST /v1/previews/pr-feedback/remediation`: Contract-backed bounded preview
  feedback remediation path; dry-run before apply.
- Launchplane host-only CLI helpers: Use only when you are explicitly on the
  Launchplane host via SSH or the repo provides a concrete command. Do not
  assume a global `launchplane` binary exists on ordinary workstations.
