---
name: launchplane
description: Use for Launchplane-managed product/runtime state, secrets, config, deployments, rollout direction, product ownership boundaries, merge-train flow, and audited admin mutations. Use with github-plan when Launchplane work needs to stay aligned with a durable plan, issue graph, blockers, or rollout sequence. If authority is unknown or discovering private infrastructure access, use docs-lookup first.
metadata:
  short-description: Operate Launchplane-managed state
resources:
  - path: scripts/launchplane-service-logs.py
    kind: script
    description: Reads a bounded, redacted window of Launchplane's own retained service logs using an existing SSH identity.
  - path: references/service-log-reader.md
    kind: reference
    description: Private target selection, existing read authority, redaction, bounds and coverage limits for service logs.
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
commands:
  - name: launchplane-service-logs
    source: skill
    resource_path: scripts/launchplane-service-logs.py
    example_argv: ["uv", "run", "scripts/launchplane-service-logs.py", "--config", "<private-log-target.json>", "--since", "<RFC3339>", "--until", "<RFC3339>"]
    purpose: Reads Launchplane's own retained API or worker logs without a service mutation, credential input or new grant.
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
  - name: launchplane-merge-train-trusted-automation-propose
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "merge-train-trusted-automation-propose", "--change", "add", "--github-user-id", "<id>", "--repo", "OWNER/REPO", "--reason", "<reason>", "--source-event-id", "<stable-id>"]
    purpose: Proposes one trusted automation id change on named enrolled repositories from the active policy, keeping the record in memory, and returns the review URL without approval or apply.
  - name: launchplane-merge-train-enrollment-propose
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "merge-train-enrollment-propose", "--repo", "OWNER/REPO", "--repository-id", "<id>", "--like", "OWNER/ENROLLED", "--reason", "<reason>", "--source-event-id", "<stable-id>"]
    purpose: Proposes enrolling one repository branch with an enrolled repository's target settings from the active policy, keeping the record in memory, and returns the review URL without approval or apply.
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
  - name: launchplane-governance-projection-read
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "governance-projection-read", "--repository", "OWNER/REPO", "--base-branch", "main", "--pr", "42"]
    purpose: Reads bounded current readiness and historical admission and landing evidence without granting mutation authority.
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
    purpose: Saves a private service review for removing named stored runtime settings; this CLI exposes review and readback, not the producer's separately authorized apply.
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
    purpose: Records unknown testing facts and optional retained-operation linkage for a reviewed lane binding; refuses preview assertions.
  - name: launchplane-disposable-lane-clear
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "disposable-lane-clear",
        "--payload-file",
        "<private-file>",
        "--expected-binding-sha256",
        "<binding-sha256>",
        "--expected-observation-sha256",
        "<observation-sha256>",
        "--inspect-evidence-file",
        "<private-inspect-output>",
        "--reviewed-inspect",
      ]
    purpose: Clears only the reviewed observation for an unchanged lane binding and verifies its absence; never deletes lane resources.
  - name: launchplane-disposable-lane-prepare-rebuild
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/launchplane-write-action.py",
        "disposable-lane-prepare-rebuild",
        "--payload-file",
        "<private-file>",
        "--expected-binding-sha256",
        "<binding-sha256>",
        "--inspect-evidence-file",
        "<private-inspect-output>",
        "--reviewed-inspect",
      ]
    purpose: Requests the service's retained testing-operation capture for a reviewed lane binding; never rebuilds or proves historical effects absent.
  - name: launchplane-testing-replacement-resources-dry-run
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "testing-replacement-resources-dry-run", "--payload-file", "<private-file>"]
    purpose: Records retained preparation, excludes the retained executor and returns a service plan without allocating provider resources.
  - name: launchplane-testing-replacement-resources-apply
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "testing-replacement-resources-apply", "--payload-file", "<private-file>", "--expected-plan-digest", "<plan-sha256>", "--dry-run-evidence-file", "<private-review-file>", "--reviewed-dry-run", "--idempotency-key", "<original-key>"]
    purpose: Submits the reviewed original-key testing allocation and independently reads back its resource binding.
  - name: launchplane-testing-replacement-resources-release
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "testing-replacement-resources-release", "--payload-file", "<private-file>", "--expected-plan-digest", "<plan-sha256>", "--dry-run-evidence-file", "<private-review-file>", "--reviewed-dry-run", "--idempotency-key", "<original-key>"]
    purpose: Releases only a reviewed initial preparation after the service proves no provider effects started.
  - name: launchplane-testing-replacement-resources-readback
    source: skill
    resource_path: scripts/launchplane-write-action.py
    example_argv: ["uv", "run", "scripts/launchplane-write-action.py", "testing-replacement-resources-readback", "--payload-file", "<private-file>"]
    purpose: Requests actual testing resource qualification for the retained operation; never repeats provider creation.
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
          argv_prefix: ["launchplane", "ingress", "health-history-summary"]
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
          argv_prefix: ["launchplane", "service", "plan-config-authority-transition"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "local-consumer", "--help"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "local-consumer", "prepare"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "local-consumer", "deliver"]
        - repository: cbusillo/launchplane
          argv_prefix: ["launchplane", "local-consumer", "receive"]
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

The repository-scoped `exceptions` in this skill's
`prefer-launchplane-helpers-over-global-cli` policy name the commands that may run through
`uv run [--extra dev] launchplane` in a verified `cbusillo/launchplane`
checkout or linked worktree, including frontend contract generation.
`ci postgres-integration` may run there too when the command itself names a
loopback database through `--database-url` or a leading
`LAUNCHPLANE_TEST_POSTGRES_URL=`. Use the session's working directory or a
single literal absolute `cd <checkout> &&` prefix; on Codex, an exec `workdir`
is not visible to the hook, so use the `cd` prefix. Other shell
directory/project overrides retain the block. Consumers without
repository-exception support keep the original block; do not bypass it through
an alternate entry point. The local gates support chained checks and output
redirection.

For the Director-approved testing local-consumer path (Q921), read the
[custody contract](https://github.com/cbusillo/launchplane/blob/main/docs/testing-local-consumer.md)
before running `uv run launchplane local-consumer prepare`, `deliver` or
`receive` from a verified Launchplane checkout. For real transfers, use a clean
primary/runtime installation at a merged default-branch commit; task worktrees
use fake isolated fixtures. These commands use existing
exact-testing-lane authority; the service enforces the credential and caller
boundary. Keep recipient files outside repositories and task evidence; never
inspect or emit their key or token values. Every other live/admin CLI command retains its helper
route below. This routing exception creates no access or grant.

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
has the required authority. Source delivery grants no access. To add or remove
one trusted automation id on named enrolled repositories, use
`merge-train-trusted-automation-propose`; to enroll repository branches, use
`merge-train-enrollment-propose`. Never hand-build a merge-train record; both
keep the active record in memory only.

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

For lane health, use `uv run scripts/launchplane-write-action.py
product-status-read`. It returns the service-computed Product
Ops sidebar tones and reasons for every recorded lane, plus product warnings. It reads the whole caller-visible inventory in one request.
Do not reconstruct colors from diagnostic reads or run frontend source locally.

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

Before diagnosing a train, read [merge-train execution](references/merge-train.md)
for phase, stack, retry, and terminal evidence requirements. Label every PR
in a stack that is ready to land, and never hand-collapse stacks.

- **Landing**: A current-head Ready handoff or the enqueue label is the whole
  landing step. Launchplane's scheduled pass runs the controller every few
  minutes for every enrolled target and lands the PR; run no driver or pass to
  speed it up. Waiting on the train needs only reading the result with
  `babysit-pr --once` or the PR's train comment.
- **Diagnosis**: Use `scripts/launchplane-write-action.py
  merge-train-controller-run-once` (`POST /v1/work-graph/merge-train/controller/run-once`)
  instead of open-coding the route. Mutating calls require an idempotency key.
- **Train Entry**: Read the target branch's enqueue label from the active
  policy. Use `uv run <github-skill-dir>/scripts/gh-pr.py --repo OWNER/REPO ready <pr>`
  instead of writing the ready comment; the train admits a current-head Ready
  without a label. Add `--waiting-for-client` until a required Client
  acceptance covers the current head. Read the GitHub
  [ready handoff contract](../github/references/cli-reference.md#ready-handoffs)
  for checks and stale-head handling. A held PR or stack child gets the
  waiting form, stays unlabeled or draft, and that stops the stack.
- **Scheduled controller**: Read the active enrolled-target policy with
  `merge-train-policy-read`. For scheduler changes, use the producer's
  [operational policy path](https://github.com/cbusillo/launchplane/blob/main/docs/merge-train-policy.md#policy-changes-and-bindings)
  with its reviewed baseline and standing lane authority. Approval follows the direction files loaded under
  Rollout Plan Alignment and
  [Q297](https://github.com/cbusillo/launchplane/issues/2961#issuecomment-6177897575).
- **Post-Merge Checkout Handoff**: After the PR merges, delegate post-merge
  default-branch freshness to `github` with the merged PR's `mergeCommitOid`
  and an explicit source worktree belonging to the landed repository. Never
  substitute a PR head, candidate, admission, observation, landing-plan, queued,
  or other intermediate SHA, and never use an unrelated working directory as the
  source worktree. That handoff uses the landed repo-local reconciler for a runtime-bound
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
Actions**. Before a mutation, follow its producer contract's dry-run or
reviewed-inspect procedure and verify the situation.

## Tools

Use `scripts/launchplane-write-action.py --help` for the installed command list.
Before selecting an operation, read the matching section of the
[write-action helper contract](references/write-action-helper-contract.md).
It owns request fields, reviewed evidence, idempotency, output bounds and
readback requirements. The producer contract remains the operation authority
under [Agent/Admin Contract](#agentadmin-contract).

| Task | Read before using the helper |
| --- | --- |
| Testing ingress | [Application testing private ingress](references/write-action-helper-contract.md#application-testing-private-ingress) |
| Disposable lane observations and rebuild preparation | [Disposable lane observations](references/write-action-helper-contract.md#disposable-lane-observations) |
| Replacement allocation and qualification | [Empty testing replacement resources](references/write-action-helper-contract.md#empty-testing-replacement-resources) |
| Product configuration and intent preflight | [Product Config](references/write-action-helper-contract.md#product-config) |
| Expected configuration metadata | [Product expected configuration](references/write-action-helper-contract.md#product-expected-configuration) |
| Odoo addon settings | [Odoo addon settings](references/write-action-helper-contract.md#odoo-addon-settings) |
| Odoo website intent | [Odoo website settings](references/write-action-helper-contract.md#odoo-website-settings) |
| Integration allowances | [Integration allowances](references/write-action-helper-contract.md#integration-allowances) |
| Testing holds | [Testing hold](references/write-action-helper-contract.md#testing-hold) |
| Product profile, environment, activity, protected artifacts, previews, reconciliation, secret bindings, testing state and rebuild | [Product environment, activity and preview reads](references/write-action-helper-contract.md#product-environment-activity-and-preview-reads) |
| Odoo target replacement operation and plan evidence | [Product environment, activity and preview reads](references/write-action-helper-contract.md#product-environment-activity-and-preview-reads) |
| Dokploy inventory | [Dokploy inventory read](references/write-action-helper-contract.md#dokploy-inventory-read) |
| Names of runtime settings and secrets | [Names-only settings and secret reads](references/write-action-helper-contract.md#names-only-settings-and-secret-reads) |
| Product repository identity | [Product repository identity](references/write-action-helper-contract.md#product-repository-identity) |
| Launchplane's own service settings | [Service settings](references/write-action-helper-contract.md#service-settings) |
| Product Client | [Client](references/write-action-helper-contract.md#client) |
| Image repository | [Image repository](references/write-action-helper-contract.md#image-repository) |
| Provider target creation and source completion | [Provider target](references/write-action-helper-contract.md#provider-target) |
| Domain and ingress planning | [Inert domain and ingress planning](references/write-action-helper-contract.md#inert-domain-and-ingress-planning) |
| Reviewed domain, ingress and route-binding repairs | [Reviewed route-authority record repairs](references/write-action-helper-contract.md#reviewed-route-authority-record-repairs) |
| Compose web host routes | [Compose web host routes](references/write-action-helper-contract.md#compose-web-host-routes) |
| Production backup authority | [Production backup authority](references/write-action-helper-contract.md#production-backup-authority) |
| Backup worker setup and qualification | [Backup worker references and capture-free preflight](references/write-action-helper-contract.md#backup-worker-references-and-capture-free-preflight) |
| Private health endpoints | [Private health endpoints](references/write-action-helper-contract.md#private-health-endpoints) |
| Health checks and dependency policy | [Health monitoring](references/write-action-helper-contract.md#health-monitoring) |
| Production promotion status and dry-run | [Product promotion status and dry-run](references/write-action-helper-contract.md#product-promotion-status-and-dry-run) |
| Odoo business hours | [Odoo business-hours policy](references/write-action-helper-contract.md#odoo-business-hours-policy) |
| Managed secret copies | [Copying a managed runtime secret](references/write-action-helper-contract.md#copying-a-managed-runtime-secret) |
| Managed runtime sync | [Managed runtime sync](references/write-action-helper-contract.md#managed-runtime-sync) |
| Generic-web deploy recovery | [Generic-Web Deploy Recovery](references/write-action-helper-contract.md#generic-web-deploy-recovery) |
| Workflow authorization retirement inspection | [Workflow-rule retirement inspection](references/write-action-helper-contract.md#workflow-rule-retirement-inspection) |
| Runtime-setting removal review | [Runtime-setting removal review](references/write-action-helper-contract.md#runtime-setting-removal-review) |
| Policy proposals | [Policy Proposals](references/write-action-helper-contract.md#policy-proposals) |
| Merge-train policy import | [Merge-Train Policy Import](references/write-action-helper-contract.md#merge-train-policy-import) |
| Repository inventory | [Repository Inventory](references/write-action-helper-contract.md#repository-inventory) |
| Product path prerequisites | [Product Path Check](references/write-action-helper-contract.md#product-path-check) |
| Pending Client review withdrawal | [Pending Client review withdrawal](references/write-action-helper-contract.md#pending-client-review-withdrawal) |
| Governance evidence | [Governance projection read](references/write-action-helper-contract.md#governance-projection-read) |

Use `scripts/launchplane-context.py` for optional structural context,
`scripts/launchplane-owner-review.py` for saved Client feedback and
`scripts/launchplane-service-logs.py` for bounded redacted service logs, under
their procedures above. `scripts/check-agent-operator-contract.py` validates
installed caller bindings offline; it does not prove deployed compatibility.
