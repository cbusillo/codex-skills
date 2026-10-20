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
    description: Validate local HTTP caller bindings and inert-planner constraints offline.
  - path: references/agent-operator-contract.md
    kind: reference
    description: Producer contract source and local HTTP caller binding guidance.
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
  - name: launchplane-domain-authority-dry-run
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "dokploy-target-repair-domain-authority-dry-run", "--context", "<context>", "--instance", "<instance>", "--reason", "<reason>"]
    purpose: Plans domain authority from fresh tracked-target and live-domain evidence without applying.
  - name: launchplane-ingress-route-dry-run
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "ingress-route-dry-run", "--payload-file", "<private-file>"]
    purpose: Verifies an exact existing ingress no-op with create, update and enable/disable disabled.
  - name: launchplane-privileged-policy-propose
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "privileged-policy-propose", "--payload-file", "<private-envelope>"]
    purpose: Creates an inert pending policy plan and returns its full Director UI review URL without approval or apply.
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
    purpose: Checks local HTTP caller bindings and inert-planner constraints offline.
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
  - name: launchplane-workflow-rule-retirement-inspect
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "workflow-rule-retirement-inspect",
        "--payload-file",
        "<private-file>",
      ]
    purpose: Inspects selected workflow authz rules' scope, management and broad flag without changing policy.
  - name: launchplane-workflow-rule-retirement-plan
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "workflow-rule-retirement-plan",
        "--payload-file",
        "<private-file>",
      ]
    purpose: Builds an inert plan for retiring selected workflow authz rules; it has no apply mode.
  - name: launchplane-runtime-setting-removal-dry-run
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "runtime-setting-removal-dry-run",
        "--product",
        "<product>",
        "--scope",
        "context",
        "--context",
        "<context>",
        "--key",
        "<setting-name>",
        "--reason",
        "<reason>",
      ]
    purpose: Saves a private service review for removing named stored runtime settings and returns its review id and digest; it has no apply.
  - name: launchplane-runtime-setting-removal-readback
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "runtime-setting-removal-readback",
        "--product",
        "<product>",
        "--scope",
        "context",
        "--context",
        "<context>",
        "--key",
        "<setting-name>",
        "--reason",
        "<reason>",
        "--review-id",
        "<review-id>",
        "--plan-sha256",
        "<plan-sha256>",
      ]
    purpose: Reads back whether stored runtime settings match a reviewed removal, by names and digests only.
  - name: launchplane-disposable-lane-inspect
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "disposable-lane-inspect",
        "--payload-file",
        "<private-file>",
      ]
    purpose: Reads one disposable lane's binding digest with every safety fact unknown; changes nothing.
  - name: launchplane-disposable-lane-collect
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "disposable-lane-collect",
        "--payload-file",
        "<private-file>",
        "--expected-binding-sha256",
        "<binding-sha256>",
        "--inspect-evidence-file",
        "<private-inspect-output>",
        "--reviewed-inspect",
      ]
    purpose: Has the service collect and store short-lived safety facts for a reviewed testing or preview lane binding.
  - name: launchplane-disposable-lane-record
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "disposable-lane-record",
        "--payload-file",
        "<private-file>",
        "--expected-binding-sha256",
        "<binding-sha256>",
        "--inspect-evidence-file",
        "<private-inspect-output>",
        "--reviewed-inspect",
      ]
    purpose: Records short-lived safety facts for a reviewed lane binding; known facts only for an exact preview.
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
      message: Launchplane authorization desired state must not be created or changed through GitHub secrets or variables. For refused or unsupported work, follow this skill's Blocked Work Continuation.
    - id: reject-raw-github-actions-secret-writes
      match:
        shell_regex: "(?i)\\b(?:gh|gh-with-env-token)\\b[\\s\\S]*\\bapi\\b[\\s\\S]*(?:(?:--method|-X)\\s*(?:put|patch|post|delete)\\b[\\s\\S]*/(?:actions/(?:secrets|variables)|environments/[^\\s/]+/(?:secrets|variables))\\b|/(?:actions/(?:secrets|variables)|environments/[^\\s/]+/(?:secrets|variables))\\b[\\s\\S]*(?:--method|-X)\\s*(?:put|patch|post|delete)\\b)"
      action: reject
      message: Raw GitHub Actions secret or variable writes can silently make GitHub an authorization authority. Use the owning product/admin contract. For refused or unsupported work, follow this skill's Blocked Work Continuation.
    - id: reject-http-github-actions-secret-writes
      match:
        shell_regex: "(?i)\\b(?:curl|wget|http)\\b[\\s\\S]*(?:(?:(?:-X|--request|--method(?:=|\\s+))\\s*(?:put|patch|post|delete)|\\b(?:put|patch|post|delete)\\b)[\\s\\S]*/(?:actions/(?:secrets|variables)|environments/[^\\s/]+/(?:secrets|variables))\\b|/(?:actions/(?:secrets|variables)|environments/[^\\s/]+/(?:secrets|variables))\\b[\\s\\S]*(?:(?:-X|--request|--method(?:=|\\s+))\\s*(?:put|patch|post|delete)|\\b(?:put|patch|post|delete)\\b))"
      action: reject
      message: Direct HTTP writes to GitHub Actions or environment secrets and variables bypass the authorization-authority guardrail. For refused or unsupported work, follow this skill's Blocked Work Continuation.
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
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "ci", "postgres-integration"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "action-pins", "check"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "action-pins", "update"]
      action: require_preferred
      message: Do not assume a global `launchplane` binary on ordinary workstations. Use the bundled helpers unless you are explicitly on a host-only Launchplane context with a repo-provided command. A repository gate in a Launchplane checkout is allowed only when the hook can see that checkout; Codex hooks see the session directory, not a command's `workdir`, so start the command with `cd /absolute/checkout && `.
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
`odoo-ownership check`, `action-pins check|update`, and
`ci unittest-shard local|plan|run` gates may run through
`uv run [--extra dev] launchplane` in a verified `cbusillo/launchplane`
checkout or linked worktree, including frontend contract generation.
`ci postgres-integration` may run there too when the command itself names a
loopback database through `--database-url` or a leading
`LAUNCHPLANE_TEST_POSTGRES_URL=`. Use the session's working directory or a
single literal absolute `cd <checkout> &&` prefix; on Codex, an exec `workdir`
is not visible to the hook, so use the `cd` prefix. Other shell
directory/project overrides retain the block. All live/admin CLI commands
retain their helper route below. Consumers without
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

Read [the producer contract and caller bindings](references/agent-operator-contract.md)
before changing helper routes or choosing an operation surface.

### Agent Policy Proposals

For a prepared access-policy, merge-train policy or backup-policy proposal, read
[the proposal helper contract](references/write-action-helper-contract.md#policy-proposals)
and use `launchplane-write-action.py privileged-policy-propose --payload-file
<private-envelope>`. Access/merge-train proposals require an explicit managed
proposer grant for the configured `local_operator`; the Director approves it
separately through the Access policy card. Backup proposals reuse existing
exact-lane backup planning authority and open the product's production Actions
card. Submit no proposal until the service supports its descriptor and the caller
has the required authority. Source delivery grants no access.

Creation is pending and inert. Director questions and handoffs that send the
Director to a page include a full clickable URL, never a bare UI path. Use the
helper's `review_url` for proposal review, following
[talking with the Director](../references/talking-with-the-director.md#carry-context).
Keep private URLs in destinations allowed to know them. Approval
stays in the signed-in UI and the proposer can never approve or apply. Reuse the
private envelope's stable source event when a response is uncertain. This path
uses private `local_operator` config, independent of terminal-agent credentials.

### Contract-Backed Lifecycle And Repair Routing

For lifecycle retirement, managed authorization reconciliation, and stable-lane
repair, resolve the requested operation from the producer contract before
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
apply without every contract-required reviewed-evidence field and an
apply-eligible result. For approval and standing authority, follow
[task scope](../references/execution-scope.md) and the direction files loaded
under Rollout Plan Alignment below. Detached application retirement must
preserve zero authority writes and is complete only when candidate absence is
proved.

## Rollout Plan Alignment

For Launchplane rollout, runtime, product-boundary, merge-train, or admin
work, do not continue from the latest operational finding alone. Before the next
slice, state how it fits the active Launchplane plan, issue graph, rollout
sequence, or product ownership boundary.

Before Launchplane operational work, including apply, mutate, repair,
retirement, policy changes, workflow design or grant proposals, read the
Director's overall direction and `cbusillo/launchplane`'s `DIRECTION.md` at
`origin/main`. Do this even when working from a product repository.

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
The output contains Client prose: unless its publication is authorized, keep
it only where [task artifacts](../references/repo-cleanup.md#task-artifacts-and-retention)
go, the session scratchpad or the host's named task-evidence location. A failed read means unavailable, not no feedback.
The PR watcher uses this reader to verify its GitHub projection and receipt.

## Stable Deploy Identity

Before preparing or repairing an application-target deployment, read
[stable deploy identity](references/deploy-identity.md). Publish both an
immutable digest and immutable SHA tag before deployment; never use a floating
tag. Resolve the target category from Launchplane context or the admin
surface. Repair missing references through the product repository's build/deploy
workflow and delegate protected workflow dispatch and watching to `github`.

## Runtime Management (Admin)

When diagnosing a Launchplane operation, first read its service logs with
`scripts/launchplane-service-logs.py --config <private-log-target.json>
--since <RFC3339> --until <RFC3339> [--operation-id <id>] [--trace-id <id>]`.
Read [the service-log reader contract](references/service-log-reader.md) before
using it. It reuses an existing SSH read path, emits bounded redacted JSON and
changes no service state. Removed containers and rotated logs remain unknown;
an empty filtered tail never proves that an operation did not run.

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
  Preserve the trace and source URL, then follow
  [Blocked Work Continuation](#blocked-work-continuation).
- **GitHub Is Not Authorization Authority**: A GitHub workflow, Actions secret,
  repository or environment variable, OIDC role, or CI job is never evidence
  that a denied Launchplane action is authorized. Do not author, edit, extend,
  retarget, or dispatch a workflow to carry a call Launchplane denied.
  Repository workflow content is routing metadata, just like
  `.github/github.json`; Launchplane DB/service records remain authority.
- **Unsupported Helper Coverage**: Follow
  [Blocked Work Continuation](#blocked-work-continuation) for a missing helper
  command. Use the supported service/UI surface; do not synthesize runtime
  records from issue text, examples, workflow defaults or local files.
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
  8. **Apply** with a concrete reason after the dry-run succeeds, under the
     authority established by [task scope](../references/execution-scope.md).
  9. Inspect returned `next_actions` and complete required follow-up actions;
     product-config apply can update Launchplane records before the live target
     runtime has been synced.

Agents may guide the admin, prepare request shape, summarize redacted dry-run
evidence, and report trace IDs/status. Agents must not collect plaintext secret
values in chat, issues, PRs, docs, logs, or helper output, and must not bypass
Launchplane by editing provider configuration directly.

### Blocked Work Continuation

Apply the refusal rules in the direction files read under Rollout Plan
Alignment. Keep the actual refused operation stopped and preserve its trace, source URL and any uncertain-effect
evidence. Those sources own the distinction between a wrong refusal and one
that correctly protects a stop boundary, and the required response to each.
Use `github-plan` to track a required repair and any native dependency on it.
Reconcile uncertain effects before reexecution.

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
For a Director with Launchplane configured, missing settings, unavailable access
or an invalid service response leave enrollment unknown. Never infer a direct
merge path from missing metadata or a failed read. A Director without Launchplane
uses the GitHub skill's [Merging](../github/SKILL.md#merging) procedure, which
establishes configuration absence locally and skips this skill and train steps.

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
- **Supervisor Handoff**: When the task authorizes a Supervisor handoff, use
  `uv run <github-skill-dir>/scripts/gh-pr.py --repo OWNER/REPO ready <pr>`
  instead of writing the ready comment. Add `--waiting-for-client` until a
  required Client acceptance covers the current head. Read the GitHub
  [ready handoff contract](../github/references/cli-reference.md#ready-handoffs)
  for checks and stale-head handling. The assigned Supervisor owns train entry.
- **Scheduled controller**: Read the active enrolled-target policy with
  `merge-train-policy-read`. For scheduler changes, use the producer's
  [operational policy path](https://github.com/cbusillo/launchplane/blob/main/docs/merge-train-policy.md#policy-changes-and-bindings)
  with its reviewed baseline and standing lane authority. Approval follows the direction files loaded under
  Rollout Plan Alignment and
  [Q297](https://github.com/cbusillo/launchplane/issues/2961#issuecomment-6177897575).
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

- `testing-private-ingress-dry-run` / `-apply` / `-readback` wrap the producer's
  application testing ingress operation. Read the
  [private ingress caller contract](references/write-action-helper-contract.md#application-testing-private-ingress)
  before use, including private files, review, original-key recovery, redaction
  and readback limits.
- `disposable-lane-inspect` / `-collect` / `-record` wrap the producer's
  disposable lane observation operation. Read the
  [disposable lane contract](references/write-action-helper-contract.md#disposable-lane-observations)
  before use, including the reviewed inspect binding, testing fact limits, no
  retry and readback limits. Clearing an observation is not offered yet.

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
- `scripts/check-agent-operator-contract.py`: Offline local caller-binding and
  inert-planner check; see [the binding guide](references/agent-operator-contract.md).
- `operator-config-diagnostic`: Redacted source-presence diagnostic for local
  admin URL and token configuration. Global options such as `--url` must come
  before the subcommand.
- `POST /v1/agent/write-intents/evaluate`: Product-config preflight surface for
  authorization and managed-secret binding evidence; never carries plaintext.
- `POST /v1/product-config/apply`: Primary product-config admin path for
  signed-in, scoped admins; dry-run before apply.
- `POST /v1/merge-train/policies/import`: Bounded helper path for
  private merge-train policy dry-run/apply. Require active-policy digest
  preflight, exact saved dry-run evidence, reviewed acknowledgement,
  idempotency, and post-apply read-back. The helper-side digest check is not
  server-enforced compare-and-swap.
- `GET /v1/work-graph/merge-train/policy`: Bounded helper read
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
  helper paths for a lane's non-production integration allowances
  (`integration-allowances-read` / `-dry-run` / `-apply`). The private payload
  carries the lane's whole allowance list, each with `integration`, `kind`
  (`dev_store`, `read_only_source` with grant `evidence`, or `pre_live`) and
  `reason`. Apply requires the saved dry-run evidence, `--expected-plan-digest`,
  reviewed acknowledgement and an idempotency key. Use the product's canonical id
  (for example `odoo-tenant-opw`); the service checks that it owns the lane.
- `GET /v1/product-config/testing-hold` and
  `POST /v1/product-config/testing-hold/apply`: Bounded helper paths
  for a testing lane's staff-testing hold (`testing-hold-read` / `-dry-run` /
  `-apply`, with `--hold` or `--lift`, `--reason` and optional
  `--purpose staff_testing|isolation_staging`). Apply requires the same
  saved-dry-run safeguards with matching purpose. Staff lifts request testing
  reconciliation; isolation-staging lifts alone do not. Read the
  [testing hold contract](references/write-action-helper-contract.md#testing-hold)
  for legacy evidence and service-derived rehearsal responses.
- `POST /v1/product-profiles/repository-identity/apply`: Bounded helper
  path that records a product's repository id from tracked inventory
  (`product-repository-identity-dry-run` / `-apply`, with `--product` and
  `--reason`). Apply requires the same saved-dry-run safeguards.
- `GET /v1/products/{product}/environments/{environment}` and
  `GET /v1/products/{product}/activity`: Bounded helper reads
  (`product-environment-read --product --environment` and
  `product-activity-read --product`) for confirming what a lane actually runs:
  the current artifact id, source commit, image digest, expected and observed
  runtime identity with its deployment record id, health status, and recent
  deployment, promotion and backup-gate events with their record ids. Settings,
  secrets, actions, URLs and provider target names are dropped.
- `GET /v1/artifacts/protected`: Bounded helper read
  (`protected-artifacts-read --product P [--context C]`) of protected artifacts:
  per-entry reason, context, instance, artifact id, source record type and id,
  and image digest. It returns sanitized warning texts, total entry and warning
  counts, and truncation flags. Image reference lists, URLs and other provider fields are
  dropped. Identifier validation and retention limits follow the
  [helper contract](references/write-action-helper-contract.md#product-environment-activity-and-preview-reads).
- `GET /v1/product-profiles/{product}`: Bounded helper read
  (`product-profile-read --product`) for who a product's Client is and its
  `production_use` and `release_on_acceptance` metadata. For release authority,
  follow the [release rule](../references/role-words.md#release-rule) and read
  service-owned evidence with `product-promotion-status-read`. It also returns
  the display name, driver, repository,
  lifecycle state and lane contexts and instances. Images, URLs, workflows and
  expected configuration are dropped.
- `GET /v1/products/{product}/path-check`: Bounded helper read
  (`path-check --product P --path testing|promote`). Use it first when asking
  whether this identity can take this product to done. It answers for the
  caller's own identity and requires `product_environment.read` on the
  product's lane contexts. It returns every ordered step as `clear`, `blocked`
  or `unknown`, with counts, a code, Launchplane's fixed description, fix kind
  (`none` for clear steps), and evidence record ids. Unknown fields and unsafe
  names or values fail closed. The read writes nothing and grants no authority;
  a clear path does not replace approval or the action's own gates.
- `GET /v1/previews/{preview_id}/history`: Bounded helper read
  (`preview-history-read`, by `--preview-id` or by `--context`, `--repository`
  and `--pr`) for confirming what a preview serves: its state, serving
  generation, and each generation's artifact, PR head SHA, image digest and
  failure stage.
- `GET /v1/product-profiles/{product}/reconcile-requests`: Bounded
  helper read (`reconcile-requests-read --product`) for what the
  event reconciler last decided for each of a product's previews and its
  testing lane: state, attempt, delivery id, last error, and the plan's
  action, reason, commit, digests and ids. When testing keeps an older build,
  `rejected_builds` lists the newer commits that failed verification. Use `--target-key <exact-target-key>`
  to select a target from the service response before the helper's output bound;
  a truncated unselected read cannot prove that testing is absent. An empty
  selection describes only the returned service list, not records outside it.
- `GET /v1/products/{product}/secret-bindings`: Bounded helper read
  (`product-secret-bindings-read --product`) of a product's runtime secret
  binding metadata: binding key, name, scope, context, instance, declared
  class, sharing reason and current `version_id`. It never returns a value or
  ciphertext, and lists only bindings the caller's `secret.list` access covers,
  so an empty list does not prove absence. Use a binding's context, instance
  and `version_id` as `copy_from` in a product-config secret entry to copy a
  product's own stable-lane secret into another of its lanes without
  collecting the value; see the
  [write-action contract](references/write-action-helper-contract.md#copying-a-managed-runtime-secret).
- `GET /v1/contexts/{context}/runtime-settings` and `/secrets`, with their
  `/instances/{instance}/...` variants: names-only helper reads
  (`context-runtime-settings-read`, `instance-runtime-settings-read`,
  `context-secrets-read`, `instance-secrets-read`). Supply `--context` and,
  for instance commands, `--instance`. Settings show `keys` and `withheld_keys`
  by record scope; secrets show name, integration, scope and status. These
  inventories can include inherited global records and reflect caller access.
  Read the [names-only contract](references/write-action-helper-contract.md#names-only-settings-and-secret-reads)
  for output bounds and the service's plain-value transport limitation.
- Native testing rebuild preflight and submission (`testing-rebuild-dry-run` /
  `testing-rebuild-submit`) reuse `POST /v1/drivers/odoo/target-replacement-plan`
  and `POST /v1/drivers/odoo/target-replacement-apply`. The fixed testing-only
  intent, reviewed evidence, audit reason and recovery protocol are in the
  [helper contract](references/write-action-helper-contract.md#product-environment-activity-and-preview-reads).
  These are bounded helper bindings and require deployed native service support.
- `GET /v1/drivers/odoo/target-replacement/operations/{operation_id}`: Bounded
  helper read (`target-replacement-operation-read --operation-id`)
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
  When supplied by the service, `result.recovery` retains its closed,
  value-free recovery classification; see the
  [helper contract](references/write-action-helper-contract.md#product-environment-activity-and-preview-reads).
  Omitted nullable agreement fields remain unknown. Journal absence or zero
  journaled effects never proves absent provider effects or safe release.
- `POST /v1/drivers/odoo/target-replacement-plan`: Bounded helper
  read (`target-replacement-plan-read --product --instance`) for what an Odoo
  target replacement on that lane would do, before any apply. Add
  `--artifact-id` with `--source-git-ref` to plan with a candidate artifact
  instead of the lane's current one. The route builds a plan and writes
  nothing. It returns plan status, strategy, data source mode, the artifact it
  evaluated, blocker codes, and env-key names only: the keys
  it would deliver (`delivered_runtime_keys`, `null` when the service does not
  report them yet) and retire, the keys each blocker is about, and the current
  target's env keys and missing volume keys, plus step ids and statuses.
  Blocker, step and warning text, domains, target names and ids, and volume
  values are dropped and listed by path. The service authorizes it as
  `odoo_target_replacement_plan.read` on the lane.
- `POST /v1/authz-policies/workflow-rule-retirement-plan`: Bounded
  helper read (`workflow-rule-retirement-inspect` /
  `workflow-rule-retirement-plan --payload-file`) for workflow authz rules you
  may retire. The private file names the active policy record, revision and
  digest and up to 50 rule fingerprints. It returns each rule's scope,
  management and broad flag, plus blockers. The plan adds inert candidate
  digests. Nothing is written and there is no apply mode. Launchplane does not
  know which workflows use a rule; review workflow sources first. Read the
  [helper contract](references/write-action-helper-contract.md#workflow-rule-retirement-inspection).
- `POST /v1/admin/runtime-settings/removal`: Bounded helper path
  (`runtime-setting-removal-dry-run` / `-readback`, with `--product`,
  `--scope`, `--context`, `--instance`, one `--key` per setting name, and
  `--reason`) for the Director agent to review removing stored plain runtime
  settings. Output holds names and digests only, never values or the reason.
  A dry run saves a private service review and returns its `review_id` and
  `plan_sha256`. Readback repeats the same keys and reason with that review id
  and digest. There is no apply here. Apply belongs to the signed-in admin,
  and a dry-run digest does not authorize it. Read the
  [helper contract](references/write-action-helper-contract.md#runtime-setting-removal-review).
- `POST /v1/product-profiles/{product}/owner`: Bounded helper path
  that records or clears a product's Client (`product-owner-dry-run` /
  `-apply`, with `--product`, `--github-login` or `--clear`, and `--reason`).
  Apply checks the Client has not changed since the review, then reads the
  profile back.
- `POST /v1/product-profiles/{product}/image-repository`: Bounded
  helper path that moves a product's image repository to the GHCR
  package named after its repository (`product-image-repository-dry-run` /
  `-apply`, with `--product`, `--image-repository` and `--reason`). The dry run
  shows the repository before and after and each lane's current artifact.
  Apply sends the reviewed starting repository and stops when the profile has
  moved since the review, then reads the profile back.
- `POST /v1/dokploy-targets/setup`: Bounded helper path that creates a
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
  workflow. `dokploy-target-create-application-dry-run` / `-apply` create a
  conventional testing application in new placement, optionally with a new
  service-named state volume; apply reuses the saved storage plan and needs the
  same `--idempotency-key` on every retry. `-readback` and `-retain` recover an
  uncertain allocation from that evidence and key. Read
  [Provider target](references/write-action-helper-contract.md#provider-target)
  before any of them.
- `dokploy-target-repair-domain-authority-dry-run` uses that setup route with
  `operation: repair-domain-authority` and fixed `mode: dry-run`.
  `ingress-route-dry-run` uses the helper
  `POST /v1/drivers/ingress/route-apply` with fixed `ingress.mode: dry-run`.
  Read [inert domain and ingress planning](references/write-action-helper-contract.md#inert-domain-and-ingress-planning)
  before either command. They use fresh service records and verify exact live
  identity/domain evidence privately; neither command can apply.
- For bounded domain-authority repair, an existing-ingress no-op audit, or
  record-only route-binding reconciliation, read
  [reviewed route-authority record repairs](references/write-action-helper-contract.md#reviewed-route-authority-record-repairs).
  `route-authority-discover` and `route-binding-read` obtain fresh scope/CAS
  evidence; the separate apply commands require saved matching dry-run evidence,
  a reviewed digest, idempotency and readback. They add no service authority.
- `GET /v1/production-backup-authority` and
  `POST /v1/production-backup-authority/apply`: Bounded helper paths
  for a production lane's backup policy and targets
  (`production-backup-authority-read` / `-dry-run` / `-apply`). The private
  payload carries the Proxmox coordinates; output shows only record ids,
  revisions, kinds and the `authority_digest` apply is bound to.
- `POST /v1/product-profiles/health-monitoring/apply`: Bounded helper
  path for one exact lane's health check (`health-monitoring-dry-run` / `-apply`).
  Supply `--product`, `--context`, `--instance`, `--check-name`, `--check-kind`
  (`public_http` or `private_http`), `--monitoring-intent` (`public`, `private`,
  or `prelaunch`), `--enabled` / `--no-enabled`, `--require-runtime-identity` /
  `--no-require-runtime-identity`, and `--reason`. A private check may name an
  existing `--private-endpoint-key`; endpoint URLs and topology fields are never
  accepted. Optional `--dependency-mode` with `--dependency-source` sets the
  lane's dependency coverage policy; omitting them preserves it. Apply requires saved dry-run evidence, `--expected-plan-digest`,
  `--reviewed-dry-run` and an idempotency key. It checks the endpoint before
  sending, then reads back the exact check and lane intent with private endpoint
  comparison. Receipts omit URLs and free-text reasons. Policy verification
  does not prove monitor observations or sustained cadence. Read the
  [helper contract](references/write-action-helper-contract.md#health-monitoring)
  before using it.
- `GET /v1/private-health-endpoints/records` and
  `POST /v1/private-health-endpoints/apply`: Bounded helper paths for
  the private health endpoint record a lane's `private_http` health check names
  (`private-health-endpoint-read --product --context [--instance]` /
  `-dry-run` / `-apply`, with `--payload-file` and `--reason`). The private
  payload carries the endpoint key, scope and URL, with optional public
  `tls_ca_pem` for testing HTTPS only; output redacts URLs and certificate
  contents, showing only a CA digest when supplied. Read the
  [private endpoint contract](references/write-action-helper-contract.md#private-health-endpoints)
  for validation and scoped-trust limits. Apply requires the saved dry-run evidence,
  `--expected-plan-digest`, reviewed acknowledgement and an idempotency key,
  then reads the record back and compares its URL and scoped CA privately.
- `GET .../environments/prod/promotion-status` and
  `POST .../environments/prod/promotion/dry-run` under `/v1/products/{product}`:
  Bounded helper reads and dry-runs (`product-promotion-status-read`,
  `product-promotion-dry-run`) for whether a testing-to-prod promotion could
  run. The dry-run takes no backup and deploys nothing. No helper command sends
  a live promotion.
- `POST /v1/work-graph/merge-train/controller/run-once`: Preferred merge-train
  controller path; call repeatedly to advance one safe phase at a time.
- `POST /v1/product-review/withdrawal`: `product-review-withdrawal-dry-run`,
  `-apply` and `-readback` withdraw only a pending labeled Client review request
  on the Director's recorded decision. Refuses after a current-head Client
  decision, adds no grant and records no acceptance. Read
  [pending review withdrawal](references/write-action-helper-contract.md#pending-client-review-withdrawal)
  before use; never hand-remove the label.
- `POST /v1/previews/pr-feedback/remediation`: Contract-backed bounded preview
  feedback remediation path; dry-run before apply.
- Launchplane host-only CLI helpers: Use only when you are explicitly on the
  Launchplane host via SSH or the repo provides a concrete command. Do not
  assume a global `launchplane` binary exists on ordinary workstations.
