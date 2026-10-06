# Launchplane Write-Action Helper Contract

This contract defines the public-safe wrapper for bounded Launchplane admin
actions. It is separate from `launchplane-context.py`: read-only context remains
optional and soft-failing, explicit write actions fail closed, and bounded
admin reads also fail closed when required configuration or authorization is
missing. Command examples run from the `launchplane` skill directory.

Projected operation paths are resolved from the vendored
`agent-operator-contract.json` through `launchplane_contract.py`. Run
`check-agent-operator-contract.py` to verify schema, semantic digest,
public-safety, operation semantics, protected workflows, invariant coverage, and
helper bindings without network access. This is local consistency evidence, not
proof of upstream freshness.

Merge-train policy read and import, repository inventory, product expected configuration,
generic-web deploy recovery, Odoo addon settings, and the lane-setup and
promotion commands below are intentionally listed as bounded local extensions because their
routes are not in the current vendored projection. The conformance gate fails
if those routes later appear upstream so migration cannot leave duplicate route
authorities behind.

The helper lives at `scripts/launchplane-write-action.py`.

Private payload files must be outside the active repository. The helper checks
both the file's location after resolving ancestor directory aliases and its
fully resolved target, comparing ancestor directories by filesystem identity
when their path spellings differ. A repository-local link to an external payload
and an external link to repository-local data are both refused; an external payload,
including an external link to another external file, is accepted.

## Read budgets and timeout diagnostics

`product-promotion-status-read` defaults to a bounded 30-second HTTP socket
timeout to accommodate complete release-checklist compilation. Other reads,
including the read-only POST used by `target-replacement-plan-read`, keep their
10-second default. An explicit `--timeout` before the subcommand overrides
the budget. Write and dry-run commands retain their existing budgets; the
controller budget is described in [Controller](#controller).

A direct or URL-wrapped socket timeout returns `unavailable` with
`client_timeout` and `timeout_seconds`. It means the client budget expired,
not that Launchplane is down. No response trace was received; retry the read
with a longer explicit budget. Other connection failures, HTTP refusals,
configuration failures and invalid projections keep their distinct diagnostics.
The helper does not retry automatically.

## Merge-train enrollment read

`merge-train-policy-read --repo OWNER/REPO` calls the complete active-policy
`GET /v1/work-graph/merge-train/policy` read using existing private admin config
and the service's standing `merge_train.policy_targets` permission. Omitting
`--repo` resolves the origin remote in `--repo-root` (default current directory).
No grant, credential resolution, policy mutation or train entry occurs.
The endpoint is a bounded local extension, absent from the vendored projection.

On success, `status=available` and `result` contains `source=launchplane`,
`status=enrolled|not_enrolled`, boolean `enabled`, per-branch `targets`
(`baseBranch` and `readyLabel`), policy record id/time/digest, and a trace id.
The projection drops other repositories, authorization and credential source
metadata. Matching ignores repository case; distinct branches remain separate.
Only a complete active-policy response proves absence. Missing config, denial,
transport failure, duplicate targets or malformed policy mean unknown to callers.
The snapshot emits `launchplane.mergeTrain.status=unknown` and `enabled=null`
on those failures, independently of `github.json`, with a compact safe `reason`
code for diagnosis. A read grants no merge authority.

## Odoo addon settings

`odoo-addon-settings-dry-run` and `odoo-addon-settings-apply` call
`POST /v1/product-config/odoo-addon-settings/apply` to set an Odoo lane's
Shopify addon settings on its instance-override record. The Launchplane
operation is `apply_odoo_addon_settings`, projected by the vendored contract.

- The private payload file, outside the repository, holds `schema_version`,
  `product`, `context`, `instance`, `reason`, and a `shopify` block with
  `shop_url_key`, `api_version`, `api_token_secret_binding_id`,
  `webhook_key_secret_binding_id` and boolean `test_store`. Any other field,
  including a plaintext `api_token` or `webhook_key`, is refused before any
  request is sent.
- Create the secrets first through product-config for the exact lane, with
  binding keys `ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN` and
  `ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__WEBHOOK_KEY`.
- Save the dry-run output. Apply requires `--reviewed-dry-run`,
  `--expected-plan-digest` equal to the saved `plan_sha256`,
  `--dry-run-evidence-file` for the same product, context and instance, and
  `--idempotency-key`.
- Output shows setting names, change actions, presence, binding references and
  non-secret literals only. A literal on any other setting fails closed.
- Apply changes the record, not the database. Check `read_back_matches`, then
  run Odoo post-deploy for the lane and verify the settings there.

## Integration allowances

`integration-allowances-read`, `integration-allowances-dry-run` and
`integration-allowances-apply` call `GET /v1/product-config/integration-allowances`
and `POST /v1/product-config/integration-allowances/apply`. A non-production lane
may hold an integration's settings only when an allowance recorded here says why.
These routes are local extensions until the vendored artifact is refreshed.

- `integration-allowances-read --product --context --instance` returns the lane's
  allowances. Use the product's canonical id; the service refuses a lane the
  product doesn't own.
- The private payload file holds `schema_version`, `product`, `context`,
  `instance`, `reason`, and `allowances`: the lane's whole list, each entry with
  `integration`, `kind` (`dev_store`, `read_only_source` or `pre_live`), `reason`
  and optional `evidence` (required for `read_only_source`). An omitted
  integration is removed. Any other field is refused before any request is sent.
- Save the dry-run output. Apply requires `--reviewed-dry-run`,
  `--expected-plan-digest` equal to the saved `plan_sha256`,
  `--dry-run-evidence-file` for the same product, context and instance, and
  `--idempotency-key`.
- The service refuses allowances on a production lane, `pre_live` outside testing
  and dev, and a target record changed since the review.

## Testing hold

`testing-hold-read`, `testing-hold-dry-run` and `testing-hold-apply` call
`GET /v1/product-config/testing-hold` and
`POST /v1/product-config/testing-hold/apply`. While site staff test a product's
testing lane, the hold keeps event-driven deploys off it. These routes are local
extensions until the vendored artifact is refreshed.

- `testing-hold-read --product --context --instance` returns the lane's hold or
  `null`.
- Dry-run and apply take `--product`, `--context`, `--instance`, `--hold` or
  `--lift`, and `--reason` (the hold's reason, or the audit reason for a lift).
  Nothing in the request is secret, so there is no payload file.
- Save the dry-run output. Apply requires `--reviewed-dry-run`,
  `--expected-plan-digest` equal to the saved `plan_sha256`,
  `--dry-run-evidence-file` for the same product, lane and hold direction, and
  `--idempotency-key`.
- Output shows the action, the hold before and after (reason, recorder and
  time), read-back, and whether lifting requested a testing reconcile.

## Product environment, activity and preview reads

`product-environment-read` and `product-activity-read` call
`GET /v1/products/{product}/environments/{environment}` and
`GET /v1/products/{product}/activity`; `preview-history-read` is below. They let an agent confirm what a
merge-triggered deploy shipped without an admin signing in. These routes are
local extensions until the vendored artifact is refreshed.

- `product-environment-read --product --environment` returns the lane's
  identity, `target.artifact` (artifact id, source commit, image repository and
  digest, source build run), expected and observed runtime identity (deployment
  record id, artifact id, source ref, image reference, deployed time) with the
  runtime identity status, health checks, public ingress status, trust state and
  provenance.
- `product-activity-read --product` returns up to 50 events, newest first, each
  with type, lane, action, status, time, title, summary and up to 10 record
  links; `events_truncated` says when more were returned.
- `protected-artifacts-read --product P [--context C]` calls
  `GET /v1/artifacts/protected` with the product and optional context query.
  It returns up to 100 entries with reason, context, instance, artifact id,
  source record type and id, and image digest, plus up to 50 warning texts
  sanitized by `public_operator_text`. `entry_count` and `warning_count`
  describe the full response; `entries_truncated` and `warnings_truncated`
  disclose omitted rows. Unknown entry fields and image reference lists are
  dropped. Artifact ids may be registry references with an optional SHA-256
  digest suffix; credential-bearing userinfo is refused in ids and warning text.
  Empty ids remain valid for preview feedback protecting image references only.
  Nonempty image digests must be SHA-256, and lane and record names cannot carry
  userinfo. Malformed retained fields fail the read. This diagnostic projection
  is not a complete registry-cleanup retention set when truncated.
- `product-profile-read --product` calls `GET /v1/product-profiles/{product}`
  and returns the product's Client GitHub login and review label, `production_use`,
  lifecycle state, display name, driver, repository and lanes (context and
  instance only). `prelaunch` is the only value that skips Client release
  review. A malformed or secret-looking value fails the whole read.
- An odd value in an optional field (for example a status Launchplane added
  later) is dropped to `""` and listed by path in `dropped_field_paths`, with
  `dropped_field_count`; an event without a usable id or type is left out and
  counted in `omitted_event_count`. A value that looks like a secret still fails
  the whole read.
- `preview-history-read` calls `GET /v1/previews/{preview_id}/history`. Pass
  `--preview-id`, or `--context`, `--repository OWNER/REPO` and `--pr`, which
  derive the id the way Launchplane does; giving both or neither is refused. It
  returns the preview's state and generation ids, and up to 20 generations
  newest first, each with state, times, artifact id, PR head SHA, source map,
  deploy and verify status, failure stage and summary, and runtime identity
  including the image digest. The same drop and omit rules apply.
- `reconcile-requests-read --product` calls
  `GET /v1/product-profiles/{product}/reconcile-requests`. It returns up to 50
  targets, each with state, times, counts, delivery id, last error and the
  plan fields the reconciler is known to write: action, reason, hold, commits,
  artifact ids, digests, operation and plan ids, the preview URL's scheme and
  host only, the Client-review flag, and the names of omitted or missing
  integration keys (as `omitted_integration_keys` and `missing_keys`). Operation
  ids include `queued_operation_id`, `active_operation_id`,
  `deployed_operation_id` and `last_failed_operation_id`. A testing
  deploy plan also keeps `deploy_operation_status`, `deploy_status`,
  `post_deploy_status`, `deployment_record_id` and `deploy_key_sha256` (SHA-256
  of the exact deploy idempotency key, never the raw key). Compare the operation
  status and digest to distinguish a replay from a new deploy attempt. A failed
  testing deploy also keeps `last_failed_error_code` and `last_failed_error_summary`.
  The service redacts that summary, and the helper's summary rules then apply
  to it the same way they do to `last_error`. Any other plan field,
  such as rejected-build error text or PR feedback, is dropped and counted under
  `requests[].last_plan.<unlisted field>`; the same drop and omit rules apply.
- `product-secret-bindings-read --product` calls
  `GET /v1/products/{product}/secret-bindings` with no query. It returns up to
  200 of the product's runtime secret bindings, each with `binding_key`,
  `name`, `scope` (`context` or `context_instance`), `context`, `instance`,
  `secret_class`, `sharing_reason` (kind, reason, evidence, recorded by and
  at) and the current `version_id`. The service needs `product_profile.read`
  and lists only bindings covered by the caller's `secret.list` access; preview,
  removed-lane, global and worker stores are never listed. A field named like a
  value, ciphertext or other secret fails the whole read, as does a
  secret-looking value in any field, including one that would be dropped; any
  other unlisted field is dropped and counted under
  `bindings[].<unlisted field>`, and a binding without a usable key, context
  or scope is omitted and counted.
- `target-replacement-operation-read --operation-id` calls
  `GET /v1/drivers/odoo/target-replacement/operations/{operation_id}` with no
  query. The service authorizes it as `operations.read` on product
  `launchplane` for the operation's context and instance, or as
  `odoo_target_replacement_apply.execute` on the operation's product, context
  and instance. The read grant returns a structured view without the request
  or embedded result; the execute grant returns the full service record. The
  helper projects either view. It returns the operation's id, product,
  context, instance, status, phase, attempt, deployment record id, the
  requested artifact id, created, updated, started, heartbeat and finished
  times, `error_code`, a bounded `error_description` using the existing
  public-summary validator (the descriptive word `credential` is redacted),
  and validated
  `error_detail_keys` (env-key names only); and, once a result exists, the deploy, post-deploy,
  health, canonical and logo statuses, the deployment and release tuple ids,
  artifact id and image digest. Free-text error messages, on the operation and
  on the result, are dropped rather than filtered: they can name hosts, provider
  targets and settings that no filter reliably catches. Request settings,
  idempotency material, lease holder, authorization and cancellation evidence,
  verification and override evidence, URLs, the image repository and provider
  target names are also dropped. Each dropped non-empty field is listed in
  `dropped_field_paths`; an unknown field is listed as
  `<unlisted field>`. A secret-looking value in a kept field fails the read.
- `target-replacement-plan-read --product --instance` calls
  `POST /v1/drivers/odoo/target-replacement-plan` with
  `{"schema_version": 1, "product": P, "replacement": {"product": P, "instance": I}}`
  and no idempotency key. The service builds the plan read-only and authorizes
  it as `odoo_target_replacement_plan.read` on the lane's product, context and
  instance. It returns `plan_status`, product, context, instance, strategy,
  expected artifact id and source ref, data source mode, the target, target-id
  and inventory found flags, `allow_empty_data`, `retired_provider_keys`,
  `delivered_runtime_keys` (`null` when the service does not report the list),
  `blocker_codes`, `blocker_keys` by code, `blocker_count`, `warning_count`,
  the current target's `env_keys` and `required_volume_keys_missing`, and each
  step's `step_id` and `status`. Every key list keeps names that match
  `[A-Za-z_][A-Za-z0-9_]*` only. Blocker, step and warning text, the next
  target's name and domains, the approval issue URL, and the current target's
  id, name, domains and live volume values are dropped and listed in
  `dropped_field_paths`, as are names that are not env keys. A secret-looking
  value in a kept field fails the read.
- Path segments must be plain identifiers; anything else is refused before a
  request is sent. Runtime settings, managed secrets, available actions, URLs,
  provider target names and driver extensions are dropped from the output.

## Product repository identity

`product-repository-identity-dry-run` and `product-repository-identity-apply`
call `POST /v1/product-profiles/repository-identity/apply`. The service copies
`repository_id` and `repository_owner_id` into the product profile from the
current tracked repository inventory record; the caller never supplies ids, and a
recorded identity is never overwritten. This route is a local extension until the
vendored artifact is refreshed.

- Both commands take `--product` and `--reason`; there is no payload file.
- Save the dry-run output. Apply requires `--reviewed-dry-run`,
  `--expected-plan-digest` equal to the saved `plan_sha256`,
  `--dry-run-evidence-file` for the same product, and `--idempotency-key`.
- Output shows the repository, identity before and after (decimal ids only), the
  inventory record, revision and digest, and read-back.

## Setting up a lane for promotion

These commands record what a product needs before Launchplane can promote it
from a testing lane: its Client, the lane's provider target and lane record, and
the production backup policy. Their routes are local extensions until the
vendored artifact is refreshed. Every apply follows the same safeguards:

- Save the dry-run output. Apply requires `--reviewed-dry-run`,
  `--expected-plan-digest`, `--dry-run-evidence-file` and `--idempotency-key`.
  A payload file must be the same file the dry-run reviewed, outside the
  repository.
- Apply compares Launchplane's applied result and a fresh read-back with the
  review. It exits 0 only when both match. `accepted_unverified` (exit 1)
  means Launchplane may have written; read the record back and do not retry
  until it explains the result. `outcome_unknown` means the exchange failed
  after the POST began, including a gateway or server error (5xx or 408); a 4xx
  is Launchplane's refusal.

Where Launchplane does not bind an apply to a dry-run, the helper hashes what
the reviewer saw into `plan_sha256` and checks the record again before the
POST. That check is not server-enforced compare-and-swap.

Order for a new lane on an existing product: record the Client, create the
provider target, add the lane record with the stable-lane repair workflow, set
the lane's secrets with `product-config-dry-run` and `-apply`, then write the
backup policy for production.

### Client

`product-owner-dry-run` and `product-owner-apply` call
`POST /v1/product-profiles/{product}/owner`. Launchplane resolves the GitHub
login to a user account and its immutable id, and authorizes the change as
`product_profile.write`.

- Both take `--product`, `--github-login LOGIN` or `--clear`, and `--reason`.
  Nothing in the request is secret, so there is no payload file.
- Output shows the operation (`set`, `clear` or `unchanged`), the Client before
  and after (login and numeric id), and `plan_sha256` over product, operation,
  before, after and reason.
- Apply reads the product profile first and stops with `stale` when the
  Client changed since the review. An `unchanged` plan has nothing to apply and
  is refused. After the POST it reads the profile back.

### Image repository

`product-image-repository-dry-run` and `product-image-repository-apply` call
`POST /v1/product-profiles/{product}/image-repository`. Launchplane accepts only
the untagged GHCR package named after the product's repository, authorizes the
change as `product_profile.write`, and changes no other profile field.

- Both take `--product`, `--image-repository ghcr.io/OWNER/NAME` and
  `--reason`. Nothing in the request is secret, so there is no payload file.
- Output shows the product, its repository, the image repository before and
  after, `changed`, each lane's current artifact id and whether it is already
  in the new package, and `plan_sha256` over product, repository, before, after
  and reason. Lane artifacts are left out of the digest because a deploy may
  move them between the dry run and the apply.
- Apply reads the product profile first and stops with `stale` when its image
  repository is no longer the reviewed starting point. It sends that starting
  point as `expected_image_repository`, so Launchplane also refuses a move from
  anywhere else. An unchanged plan has nothing to apply and is refused. After
  the POST it reads the profile back and checks `image.repository`.

### Provider target

`dokploy-target-create-compose-dry-run` and
`dokploy-target-create-compose-apply` call `POST /v1/dokploy-targets/setup`
with `operation: "create-compose"`. Launchplane creates the Dokploy compose,
and the environment and project when the payload does not name existing ones,
then records the target and its provider-target row. It refuses a lane that
already has a provider target. The route authorizes `dokploy_target.plan` or
`dokploy_target.setup` on Launchplane's own service product; a denial is an
authorization result, not a reason to use another path.

- The private payload file holds `context`, `instance`, `target_name`,
  `server_id`, `reason`, `healthcheck_path` (starting `/`), a non-empty
  `domains` list, and `project_id`, `project_name` or `environment_id`.
  Optional: `schema_version`, `project_description`, `environment_name`,
  `environment_description`, `app_name`, `description`, `source_git_ref`,
  `source_type`, `compose_path`, `custom_git_branch`, `runtime_port` and
  `deploy_timeout_seconds`. With `custom_git_branch`, an explicit relative
  `compose_path` is required; Launchplane derives the git repository from the
  exclusive generic-web product profile and disables auto-deploy.
  Any other field is refused before a request is sent. The helper adds the
  operation, the service product and, on apply only, Launchplane's typed
  confirmation.
- Use the context of the product's existing lanes; stable-lane repair adds a
  lane only to an existing context.
- Output shows the plan actions for project, environment and compose, the
  health-check path, domain and route counts, the provider kind, and
  `plan_sha256` over the payload digest and plan actions. Provider ids, server
  ids, project and environment names, domains, git URLs, env-key names, the
  payload's reason and provider requests are dropped.
- Apply reads the target back through `GET /v1/dokploy-targets/inspect`
  (`dokploy_target.inspect`) and checks that the provider-target record is
  present, that every record names the created compose, and that the tracked
  target holds the reviewed domains and health-check path. These are compared,
  never printed.

`dokploy-target-complete-compose-source-dry-run` and
`dokploy-target-complete-compose-source-apply` use the same setup route with
`operation: "complete-compose-source"`. The private payload accepts only
`schema_version`, `context`, `instance` (exactly `testing`), `custom_git_branch`,
`compose_path` and `reason`. Branch and path are required and use shell-safe
relative repository syntax (letters, digits, dot, underscore, hyphen, slash).
Target ids, placement, repository URLs, domains and credential fields are
refused. The service derives the repository and requires an empty tracked
compose with exclusive product/lane ownership and matching provider binding;
it refuses configured or partial sources and never replaces a target.

Both source operations retain the existing saved-evidence safeguards: save and
review the dry-run output, then apply the exact private payload with
`--reviewed-dry-run`, `--expected-plan-digest` (the result's `plan_sha256`),
`--dry-run-evidence-file` and a stable `--idempotency-key`. The digest includes
the planned source; completion also includes a private digest of the tracked
binding, checked through inspect before apply. This helper comparison is not a
server-enforced compare-and-swap between dry-run and apply. The service rechecks
and locks its current ownership/binding during completion.

Output shows the branch and compose path and digests, while dropping URLs,
provider ids, environment values and credentials. Apply verifies the returned
plan and binding, then compares tracked and live provider git source to the
reviewed repository URL, branch and path privately. Check `read_back_matches`;
accepted-but-unverified is not success. Completion leaves the lane in place and
starts no deployment. Creation still needs stable-lane repair afterwards.

HTTP 502 `dokploy_source_partial_outcome` may follow a provider change. The
helper reports `outcome_unknown`, retains the safe trace/code and sends no
automatic retry. Require admin reconciliation before retrying under **any**
key; never recreate, replace or silently adopt the target as recovery.

### Compose web host routes

`dokploy-target-reconcile-compose-domain-dry-run` / `-apply` and
`dokploy-target-prune-compose-domain-dry-run` / `-apply` are bounded local
extensions of `POST /v1/dokploy-targets/setup`, using the corresponding
`reconcile-compose-domain` or `prune-compose-domain` operation. They act on an
existing tracked compose; they do not create targets or change the base URL.
Authorization remains `dokploy_target.plan` / `dokploy_target.setup`, with
`dokploy_target.inspect` required for apply preflight and read-back.

Provide a private JSON file outside the repository with `context`, `instance`,
`domains` and a nonempty `reason`; `schema_version` is optional. Domains are
unique lowercase hostnames (1–100); reconcile also requires `runtime_port`
(1–65535). Prune refuses that port field. Placement, target ids, credentials and
other fields are refused. Reconcile creates HTTPS web routes with Traefik's
existing default certificate (`certificateType: none`); public TLS remains the
external proxy's responsibility.

Save and review the dry-run output, then apply the exact private payload using
`--reviewed-dry-run`, `--expected-plan-digest` (the result's `plan_sha256`),
`--dry-run-evidence-file` and a stable `--idempotency-key`. The helper digest
binds the private payload and tracked compose id. Before apply, inspect must
still identify that compose consistently across the tracked and provider-target
records. This is a helper check, not server compare-and-swap.

Output shows operation, scope, counts, port and digests only; domain names,
provider ids and warnings, and the reason stay private. Apply inspects again to
confirm the tracked domain addition/removal, preservation of unrelated tracked
domains, and provider-reported hosts (plus HTTPS and port for reconcile).
`read_back_matches: true` proves that record/provider read-back, not a network
request, public TLS, upstream certificate settings or the site's runtime identity.
Prune verification conservatively refuses to report success while any provider
route still reports a requested host. The current inspect service omits an empty
domains list; the helper treats that omission as no domains, relying on its
compose inspection to enumerate provider routes. Verify the network path separately before
cutover. Stop on `accepted_unverified` or `outcome_unknown`; inspect before any
retry, because a route may already have changed. Apply HTTP 400
`invalid_dokploy_target_setup` also reports `outcome_unknown`: that service code
can follow partial provider changes, not just input validation. No automatic apply retry runs.
Live-site applies and base-URL changes retain their Director and release gates.

### Lane record

Adding a lane to an existing product is the contract-backed
`apply_product_stable_lane_repair` operation. Its only supported surface for
agents is the protected `Product Onboarding Manifest (Advanced)` workflow in
`cbusillo/launchplane` with `operation: stable-lane-repair`: dry-run first,
then apply with the dry-run's `reviewed_plan_sha256`. Dispatch and watch it
through the `github` skill and `github_workflow_babysit.py`; this helper has no
command for it. It needs the provider target first, with the lane's domain.

### Production backup authority

`production-backup-authority-read`, `production-backup-authority-dry-run` and
`production-backup-authority-apply` call `GET /v1/production-backup-authority`
and `POST /v1/production-backup-authority/apply`. They are authorized as
`production_backup_authority.read` and `.write` on the policy's instance.

- `production-backup-authority-read --product --context --instance
  --promotion-action` returns the authority's state (`ready`, `missing`,
  `invalid`, `stale` or `retired`), reason codes (its free-text summary is
  dropped), and the policy and target
  summaries with their record ids. Use those ids as
  `expected_current_policy_record_id` and `expected_current_target_record_ids`
  for the next revision.
- The private payload file holds `policy`, `targets`,
  `expected_current_policy_record_id`, `expected_current_target_record_ids`
  and optional `schema_version`. The policy names `product`, `context`,
  `instance` and `promotion_action`; a generic-web promotion's backup gate uses
  `generic_web_prod_promotion.execute`. Leave record ids and digests blank:
  Launchplane computes them. The helper sets `mode` and, on apply,
  `reviewed_authority_digest`; a payload that carries either is refused.
- Launchplane binds the apply to the dry-run's `authority_digest`; pass it as
  `--expected-plan-digest`. The helper also refuses an apply whose payload
  differs from the reviewed one, or whose dry-run had nothing to apply.
- Target destinations carry the Proxmox host, account, guest id or storage
  name. They go to Launchplane only; output never shows them.
- After apply the helper checks Launchplane's applied policy and target record
  ids against the saved dry-run, then reads the authority back and checks them
  again.

### Private health endpoints

`private-health-endpoint-read`, `private-health-endpoint-dry-run` and
`private-health-endpoint-apply` call `GET /v1/private-health-endpoints/records`
and `POST /v1/private-health-endpoints/apply`. They are authorized as
`private_health_endpoint.read` and `.apply` on the record's product and
context. A lane's `private_http` health check names one of these records by its
`endpoint_key`.

- `private-health-endpoint-read --product --context [--instance]` lists each
  record's `endpoint_key`, product, context, instance, `status` (`active` or
  `disabled`) and `updated_at`. The URL and the free-text `source_label` are
  dropped.
- The private payload file holds `endpoint_key`, `product`, `context`,
  `instance` and `url`, with optional `status` (default `active`),
  `source_label` and `schema_version`. Launchplane refuses a public URL. The
  helper sets `updated_at`; a payload that carries it is refused. `--reason` is
  required on both dry-run and apply and is part of what the review binds.
- Launchplane does not bind the apply to the dry-run. The helper digests the
  payload and reason with the planned key and scope into `plan_sha256`; pass
  it as `--expected-plan-digest`. It refuses an apply whose payload or reason
  differs from the reviewed one, and sends the reviewed dry-run's `updated_at`
  so a retry with the same idempotency key replays instead of conflicting.
- After apply the helper compares the applied record, and the record read back
  from `GET /v1/private-health-endpoints/records/{endpoint_key}`, with the
  reviewed key, scope, status and URL. Output reports `url_matches_review` and
  never shows the URL.

### Product promotion status and dry-run

`product-promotion-status-read --product` calls
`GET /v1/products/{product}/environments/prod/promotion-status`. It shows
whether a testing-to-prod promotion could run: the lanes' deploy, health,
runtime-identity and trust states, whether release review is required and
approved, the number of release blockers, each operation's availability and
number of disabled reasons, and the `evidence_fingerprint`. Artifact ids,
commits, the release checklist and decision, the repository and workflow, the
live confirmation strings, and all free-text reasons are dropped; read the
reasons in the Launchplane UI.

`product-promotion-dry-run --product --evidence-fingerprint --reason
[--bump patch|minor|major] --idempotency-key` calls
`POST /v1/products/{product}/environments/prod/promotion/dry-run`
(`generic_web_prod_promotion.execute`). Launchplane checks the current evidence
against the fingerprint, creates no backup, deploys nothing, and records the
accepted dry-run for this identity, fingerprint and bump. Output keeps the
per-step statuses and drops artifacts, record ids, target names and error text.

No helper command sends a live promotion. Launchplane's driver route refuses a
live promotion from an admin's or agent's token, and its workflow-dispatch route
starts a product-owned workflow that calls Launchplane under a workflow
identity, which Launchplane's `DIRECTION.md` retires.

## Product expected configuration

Use `product-expected-config-dry-run --payload-file PRIVATE.json` to add or
remove declared runtime keys or managed-secret requirements through
`POST /v1/product-profiles/expected-config/apply`. The private file contains the
explicit product, reason, optional source label, and the service's
`runtime_environment_keys` / `managed_secret_bindings` metadata to add and
`remove_runtime_environment_keys` (`key`, `context`, `instance`) /
`remove_managed_secret_bindings` (`integration`, `binding_key`, `context`,
`instance`) identities to remove. A secret requirement may include
`owner_input: {label, instructions}` with an explicit context. This endpoint
accepts metadata only, never credential values.

Save the helper output outside the repository, review it against the private
input, then call `product-expected-config-apply` with the same payload file,
`--dry-run-evidence-file REVIEW.json --reviewed-dry-run --idempotency-key KEY`.
The helper binds the review to the exact metadata (excluding mode); changed
input requires another dry-run. Only apply accepts an idempotency key, so a
dry-run cannot reserve that key before the application.
Output contains record identity and added/unchanged counts, not Client instructions.
When the request removes anything, it also reports removed and absent counts for
both sections and `managed_secret_bindings_still_bound_count`: stored secret
bindings that still hold a value for a removed requirement. Removal never
unbinds or deletes a stored secret. Absent means the identity was not declared,
so a repeated removal is a no-op. Removal items must be plain strings with a
non-empty key or binding key, and an instance needs a context. A removal request
whose response lacks any removal disposition or count is refused, so it cannot
serve as apply evidence.
Read back the product profile after apply, including when a response is uncertain.

The service requires `product_profile.expected_config.apply` for the named product
in the Launchplane context. A denial remains a missing grant, not permission to
try another credential or workflow. Adding a requirement does not apply runtime
values, activate a mail server, or deploy anything. Additions do not replace
metadata on an already declared key, and one request cannot add and remove the
same identity.

For stale Launchplane-managed preview comments that cannot be replayed under a
current workflow identity, use `preview-feedback-remediation`. Run with
`--mode dry-run` first, then repeat the exact target, terminal status, reason,
related issue, and idempotency key with `--mode apply --reviewed-dry-run`.
The helper derives the service-required confirmation phrase and projects only
bounded observation and mutation evidence; it never accepts a raw comment id or
arbitrary marker.

## Configuration

The helper uses this private admin config source order. A global `--url`,
given before the subcommand, overrides the service URL from every source, and
`--env-config` replaces the default `.env` path.

1. `--config /path/to/local-operator.json`
2. environment variables in the current process
3. `~/.config/launchplane/local-operator.env`
4. `~/.config/launchplane/local-operator.json`

The committed example is fake and public-safe:

```json
{
  "service_url": "https://launchplane.example.invalid",
  "operator_token_env": "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
  "operator_subject_env": "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT",
  "operator_token_label_env": "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL"
}
```

Real token values stay in private environment or secret-manager state. The
helper never prints token values, request headers, cookies, raw request bodies,
plaintext runtime values, secret plaintext, ciphertext, provider env dumps, or
private API base URLs.

Every configured service URL is parsed and validated as an absolute endpoint
before a request is built. Non-loopback destinations must use HTTPS. Plain HTTP
is accepted only for explicit loopback hosts such as `localhost`, `127.0.0.1`,
or `::1` during local development. The helper rejects missing hosts, userinfo,
unsupported schemes, query strings, fragments, malformed ports, and control
characters. Redirects are followed only when they stay on the same
scheme/host/port origin, so bearer credentials are not replayed to a different
destination.

When `--config` is supplied, its `service_url` is the explicit write target
unless `--url` is also supplied. The helper does not also load the default `.env`
file in that case, but it does honor an explicit `--env-config` for token,
subject, and label values. When no explicit JSON config is supplied, the helper
may load `~/.config/launchplane/local-operator.env` for these keys only:
`LAUNCHPLANE_OPERATOR_URL`, `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN`,
`LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT`, and
`LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL`. The helper may also notice
`LAUNCHPLANE_PUBLIC_URL` as a diagnostic near-miss when the admin URL is
missing, but it does not use that variable as write authority.

For public-safe diagnostics, use:

```sh
uv run scripts/launchplane-write-action.py operator-config-diagnostic
uv run scripts/launchplane-write-action.py --url <operator-url> operator-config-diagnostic
```

The diagnostic reports source presence, token presence, and which source won. It
does not print token values, subjects, labels, URLs, headers, or request bodies.
Global options such as `--url` must appear before the subcommand. A diagnostic
with `status: "incomplete"` may still have a local token source; read the
`classification` field before describing the failure.

## Exit Behavior

- `0`: Launchplane accepted the request and the helper emitted a redacted
  summary. `status: "accepted_unverified"` exits 0 for the Odoo addon-settings,
  integration-allowances, testing-hold, product-repository-identity,
  generic-web deploy-recovery, repository-inventory,
  product expected-config, and merge-train policy import applies; product-config
  apply does not emit it. It exits 1 for the reviewed lane-setup
  applies (product Client, product image repository, Dokploy target, production
  backup authority, and private health endpoint). In every case the write may
  have committed; read back the active record before any retry.
- `1`: Launchplane was reached but rejected the request, or the service was
  unavailable/invalid. For `status: "outcome_unknown"`, transport failed after
  an apply POST began; read back the active record before any retry.
- `2`: The requested write action could not be attempted because local admin
  config was missing/invalid or the helper request was malformed.

Missing Launchplane config is still non-fatal for skills that only need context;
it is a fail-closed result for this helper because every command is an explicit
admin operation.

## Product Config

For helper-driven product-config work, agents should start with intent
preflight. This validates authorization and managed-secret binding policy
without accepting plaintext values:

```sh
uv run scripts/launchplane-write-action.py \
  product-config-preflight \
  --product example-product \
  --context example-testing \
  --instance web \
  --source-url https://github.com/example/repo/issues/123 \
  --reason "Preflight product-config change for issue 123." \
  --secret-binding EXAMPLE_API_TOKEN
```

The helper calls `POST /v1/agent/write-intents/evaluate` with
`intent: "product_config_apply"` and `mode: "dry_run"`. It reports only status,
trace id, record id, reason code, safe-to-execute, next action, binding keys,
and runtime key-safety finding codes.

Ad hoc plaintext secret entry should use the signed-in Launchplane UI. The
helper does not accept plaintext secrets as CLI arguments, stdin, issue text, PR
text, or chat text.

When a trusted local admin already has an explicit private payload file outside
the repo, the helper can submit the documented product-config route:

```sh
uv run scripts/launchplane-write-action.py \
  product-config-dry-run \
  --payload-file /private/path/product-config-request.json \
  --idempotency-key example-product-config-dry-run-123

uv run scripts/launchplane-write-action.py \
  product-config-apply \
  --payload-file /private/path/product-config-request.json \
  --reviewed-dry-run \
  --idempotency-key example-product-config-apply-123
```

The payload file is explicit private admin input. Do not commit it, paste it,
log it, or summarize its raw contents. It must live outside the active
repository or worktree so checked-in config and examples cannot quietly become
write payloads. Local admin apply still requires a prior matching dry-run
recorded by Launchplane.

A secret stored for one exact lane (scope `context_instance`) may carry
`secret_class` in the payload, the writer's key-safety classification for that
lane, for example `testing` for a development-store credential on a testing
lane. Launchplane refuses a class the lane does not allow. The redacted secret
results echo the stored `secret_class` alongside `action`, `integration`, and
`binding_key`.

### Copying a managed runtime secret

A product-config secret entry may carry `copy_from` instead of `value` to copy
one of the product's own stable-lane runtime secrets into the payload's target
lane inside the service:

```json
{
  "binding_key": "EXAMPLE_SYNC_API_TOKEN",
  "copy_from": {
    "context": "example-product",
    "instance": "prod",
    "version_id": "<version_id from product-secret-bindings-read>"
  },
  "secret_class": "shared_safe",
  "sharing_reason": {
    "kind": "read_only_source",
    "reason": "Testing reads the same source.",
    "evidence": "Who verified the permissions, when, and what they saw."
  }
}
```

`copy_from` has exactly `context`, `instance` and `version_id`; the helper
refuses an entry that also carries a non-null `value` (`secret_copy_with_value`)
or a malformed reference (`invalid_secret_copy_from`) before sending anything.
As in the service, a `null` `copy_from` means no copy, and a `null` `value`
beside `copy_from` means no value. The
source binding key is the destination binding key, and the destination must be
lane-exact. A declared `secret_class` and a sharing reason with evidence are
required; Launchplane does not check token permissions, so a person verifies
them and the evidence records it.

Take `version_id` from `product-secret-bindings-read`. Run
`product-config-dry-run` first, then `product-config-apply --reviewed-dry-run`
with the same file and an idempotency key; the service refuses an apply
without its matching dry-run. The redacted secret results show `copy_from`
(context, instance and version id) with `action`, `binding_key`,
`secret_class` and `sharing_reason`, through dry-run, apply and replay.

Refusals come from the service and appear only as `error_code`: a source
outside the product's stable lanes, missing, ambiguous, or whose declared class
does not allow the destination lane is `secret_copy_refused`; a rotated source
is `secret_copy_source_changed` (read the metadata again and review a fresh
dry-run); missing `secret.read` on the source is `authorization_denied`. Do not
relabel the destination to get past a class refusal. A writer authorized for
the source lane can reclassify a source verified as shareable by targeting
that same lane with `copy_from` pointing at its own current version and the new
class, reason and evidence. Reading a source and writing testing never
authorizes that production-lane write; testing-only access is not authority to
write production. A copy updates Launchplane records only; live runtime sync or
deployment stays a separate operation.

Unsupported secret source shapes must fail closed in caller guidance. Do not
translate committed secret references, provider env lookups, stdin/stdout
transport, arbitrary secret ids, or "reuse current value" requests into a
product-config request; the only supported reuse is `copy_from` above.

Unsupported runtime-authority shapes must also fail closed. Do not translate
checked-in product maps, workflow defaults, copied provider route payloads,
repository bindings, branch bindings, tenant/domain lists, lanes, provider target
ids, authz grants, or admin identities into product-config requests unless
they came from Launchplane records or explicit scoped admin input.

If a denied or unsupported operation concerns authz grants, private health
endpoint records, provider targets, route records, or admin/workflow grants,
do not widen the local helper and do not substitute GitHub CI authority. An
already-sanctioned, Launchplane-owned reconciliation entrypoint may be run
unmodified when an admin initiates it for that record. Otherwise this is a
capability gap: block and escalate the affected work to the owning
authorization-architecture issue with the denied operation, record type, and
trace ID, then continue independent work when possible.

## Merge-Train Policy Import

Merge-train policy records are runtime authority. Supply the exact service
envelope only through a private JSON file outside the active repository or
worktree. The top-level fields must be exactly `schema_version`, `product`,
`mode`, `reason`, and `record`; the helper does not rewrite the requested mode.

```sh
uv run scripts/launchplane-write-action.py \
  merge-train-policy-import-dry-run \
  --payload-file /private/path/merge-train-policy-dry-run.json \
  --expected-current-policy-digest <active-policy-digest>

uv run scripts/launchplane-write-action.py \
  merge-train-policy-import-apply \
  --payload-file /private/path/merge-train-policy-apply.json \
  --expected-current-policy-digest <active-policy-digest> \
  --expected-new-policy-digest <candidate-policy-digest> \
  --reviewed-dry-run \
  --dry-run-evidence-file /private/path/merge-train-policy-dry-run-output.json \
  --idempotency-key <stable-import-key>
```

Immediately before either POST, the helper reads
`GET /v1/work-graph/merge-train/policy-targets` and refuses the import unless
the active policy digest exactly matches `--expected-current-policy-digest`.
This is a bounded read-before-write guard, not server-enforced compare-and-swap;
the active policy can still change between the read and the POST. Admins must
therefore serialize reviewed imports and always perform active-policy read-back
after apply.

The candidate digest must be 64 lowercase hexadecimal characters, and the
record id must end with the first 12 digest characters. Apply additionally
requires a non-empty reason, a stable idempotency key, explicit review
acknowledgement, the exact candidate digest, and saved successful dry-run helper
output. That evidence binds the observed current digest plus candidate record
id, digest, status, and target count. Mismatch or malformed evidence fails
before any service request.

Saved evidence is intentionally version-strict: the helper requires the exact
standard output envelope, exact bounded request/result keys, empty records and
warnings, and the exact current/candidate projections produced by the matching
dry-run command. A helper output-shape change invalidates older evidence and
requires a fresh dry-run instead of silently accepting a partially understood
artifact.

The public result omits the policy body, repositories, branches, labels, token
source, service authorization, trusted automation ids, source, reason, private
file paths, service URL, and authorization headers. It emits only current and
candidate record metadata, digests, status, target counts, replay state, and
trace metadata. Unexpected response fields fail closed. An unreadable or unsafe
successful apply response is `accepted_unverified`; transport failure after the
apply POST begins is `outcome_unknown`. Both require active-policy read-back
before any retry.
## Repository Inventory

Repository inventory is an inert, append-only identity stream. Use the deployed
service through the bounded helper; never substitute direct database writes,
raw HTTP calls, checked-in catalogs, or workflow-owned authority. Write payloads
must be explicit private JSON files outside the active repository or worktree.

```sh
uv run scripts/launchplane-write-action.py \
  repository-inventory-read \
  --repository-id <repository-id>

uv run scripts/launchplane-write-action.py \
  repository-inventory-dry-run \
  --payload-file /private/path/repository-inventory.json \
  --idempotency-key <stable-key> \
  > /private/path/repository-inventory-dry-run.json

uv run scripts/launchplane-write-action.py \
  repository-inventory-apply \
  --payload-file /private/path/repository-inventory.json \
  --idempotency-key <stable-key> \
  --reviewed-dry-run \
  --expected-inventory-digest <inventory-digest-from-dry-run> \
  --dry-run-evidence-file /private/path/repository-inventory-dry-run.json
```

The helper overrides only `mode`. The dry-run output includes a SHA-256
`request.payload_digest` over the complete private envelope with `mode` removed,
so apply can prove that the reviewed payload, including
`expected_current_record_id`, is unchanged. Dry-run and apply require the same
stable idempotency key. Both evidence envelopes include only its
`sha256:` fingerprint, and apply rejects reviewed dry-run evidence whose
fingerprint does not match the apply key. Apply also requires explicit reviewed
acknowledgement, the server-derived inventory digest, and the saved redacted
dry-run output. Missing, malformed, stale, mismatched, or non-`would_apply`
evidence fails locally before any service request. If the private record already
embeds an inventory digest, it must match the reviewed digest exactly.

Public-safe output omits repository name, repository owner ID, source, reason, raw payload,
payload path, query ID, raw idempotency key, service URL, and authorization
headers. It may emit the redacted idempotency-key fingerprint, append-only record ID, inventory state/revision/digest,
superseded record ID, timestamps, history count, and trace metadata required for
the next exact revision. Unexpected response fields fail closed. If apply
receives HTTP success but the response cannot be safely projected, the helper
reports `accepted_unverified`; read back the current record and do not retry
until the outcome is known.

An `authorization_denied` response is an authority or capability gap. Preserve
the trace and route the blocked work to the owning authorization redesign; do
not add a grant, borrow workflow identity, or use a raw API fallback.

## Generic-Web Deploy Recovery

Generic-web deploy recovery is a bounded admin operation for recovering a
generic-web product instance from a failed deploy. Supply the private payload
only as explicit admin input in a JSON file outside the active repository or
worktree. The file contains `schema_version`, `product`, `instance`,
`original_deploy`, and `reason`. The apply request additionally requires
`expected_recovery_digest`, which the helper inserts from the `--expected-recovery-digest`
flag after verifying it against any value already in the payload and the saved
redacted dry-run evidence.

Both dry-run and apply require `--idempotency-key`. The idempotency key must be
the original deploy's idempotency key for both calls — it is sent as the request
`Idempotency-Key` header exactly as supplied.

```sh
uv run scripts/launchplane-write-action.py \
  generic-web-deploy-recovery-dry-run \
  --payload-file /private/path/deploy-recovery.json \
  --idempotency-key <original-deploy-idempotency-key>

uv run scripts/launchplane-write-action.py \
  generic-web-deploy-recovery-apply \
  --payload-file /private/path/deploy-recovery.json \
  --idempotency-key <original-deploy-idempotency-key> \
  --reviewed-dry-run \
  --expected-recovery-digest <recovery-digest-from-dry-run> \
  --dry-run-evidence-file /private/path/recovery-dry-run-output.json
```

For apply, the admin asserts that the private payload is the one reviewed
during dry-run. The helper requires:

- A non-empty `reason` embedded in the payload.
- Explicit `--reviewed-dry-run` acknowledgement.
- The `recovery_digest` emitted by the dry-run result, supplied as
  `--expected-recovery-digest` (64 lowercase hex characters).
- The saved redacted helper output from that dry-run, supplied as
  `--dry-run-evidence-file` from outside the active repository or worktree.
- A stable idempotency key (the original deploy key, used for both calls).

The helper refuses to send apply unless the evidence is the successful
generic-web recovery dry-run, its digest exactly matches the supplied digest,
its product and instance exactly match the private apply payload,
its proposed action is `adopt_observed` or `retry_original_operation`, its
provider outcome is determinate (`present` or `absent`), and `retry_safe` is
true. `hold_unknown`, `wait_for_active_lease`, `replay_completed`, unknown or
uninspected provider evidence, malformed evidence, and digest mismatches fail
locally with `reviewed_dry_run_not_apply_eligible`; no service request is sent.
The helper also refuses when the supplied digest conflicts with an
`expected_recovery_digest` already present in the payload file. When the payload
omits this field, the helper inserts the reviewed dry-run digest before apply.

The public result projects only the service's bounded recovery contract:
product/context/instance identity, reservation state and attempt, hashed target
identifiers, bounded provider classification, the proposed or executed action,
and the recovery digest. It never emits `original_deploy`, the idempotency key
value, the payload file path, raw scope, reconciliation key, provider-target
key, provider payload, service URL, or authorization headers. Unexpected
response fields fail closed. If apply receives HTTP success but the response
cannot be safely projected, the helper reports `accepted_unverified` and
requires manual verification before any retry.

## Merge Train Controller

The helper wraps the preferred merge-train route:

```sh
uv run scripts/launchplane-write-action.py \
  merge-train-controller-run-once \
  --repo example/repo \
  --base-branch main

uv run scripts/launchplane-write-action.py \
  merge-train-controller-run-once \
  --repo example/repo \
  --base-branch main \
  --mutate \
  --idempotency-key example-repo-main-controller-123
```

Dry-run calls may omit `--idempotency-key`; mutate calls require one. The helper
reports the redacted `controller_action`, durable record ids, trace id, and
compact evidence. Repeated calls should read the action before deciding whether
to run again.

Both dry-run and mutating controller calls default to a 180-second HTTP timeout;
other write and dry-run commands keep their 10-second default. Read budgets
are described in [Read budgets and timeout diagnostics](#read-budgets-and-timeout-diagnostics).
An explicit `--timeout` before the subcommand overrides the default. A client timeout returns `client_timeout`
with `timeout_seconds` and a message saying how long it waited. It does not
establish a service outage. A mutating pass may have completed after the client
stopped waiting: read the PR and controller state before any retry.

Reconciliation results retain `active_action`, `active_phase` and
`active_record_id` so a caller can identify the work holding the lease. Empty
record identifiers are returned as null. Blocked landing results retain the
landing-plan record id in `result`, and the error code in `summary`. HTTP error
bodies and non-projectable 2xx bodies retain independently validated trace ids
and error codes when present; raw error messages and unsafe identifiers are
omitted. A non-projectable response remains invalid, with no projected result.

When the controller returns `controller_action: block`, the helper preserves a
public-safe `blocking_reason` code and message plus the bounded merge-readiness
facets (`state`, reason codes, `owner_states`, technical checks, engineering
review, policy, candidate, and fence). It also preserves the service's nullable
structural-provenance summary: status, reason codes, effective base commit/tree,
and candidate, landing-plan, and provenance digests. These are diagnostic
evidence; a blocked result still requires stopping. Unexpected nested fields or secret-like
messages remain fail-closed; messages are emitted only when they satisfy the
public-summary validation contract.

Stop and report on terminal or attention actions:

- `batch_landed`
- `candidate_failed`
- `stack_unsupported`
- `block`
- `update_branch`
- `wait_for_checks`
- `wait_for_root_checks`
- `idle`

Do not hardcode repositories, labels, tokens, protected branches, private hosts,
or local file-backed product config in skill guidance or helper examples.

## Product Path Check

Use `path-check --product P --path testing|promote` first to ask whether the
caller's own identity can take a product along that path. It makes one GET to
`/v1/products/{product}/path-check` with the `path` query parameter, requiring
`product_environment.read` on the product's lane contexts. This is a bounded
local extension; it writes nothing and grants no authority.

The service's `check` becomes `result`: product, path, overall state, blocked
and unknown counts, and ordered steps with `step_id`, state, code, fixed
description, fix kind, and record ids. Clear steps use `fix: none`; other fix
kinds are `code`, `grant`, `owner_approval`, `client_acceptance`, `by_hand` and
`wait`. Counts and overall state must agree with the steps, and the response
must match the requested product and path. Unknown fields at every level,
unsafe names or values, missing fields and invalid enums fail closed. Empty
record-id lists are valid when the service has no record id for a step. Lists
are limited to 50 steps and 50 record ids per step; oversized responses fail
instead of omitting blockers. A successful read can still describe a blocked
or unknown path; inspect `result.state` and every step before proposing action.

## Output Shape

Every response is a public-safe JSON object:

```json
{
  "schema_version": "1.0",
  "status": "accepted",
  "provider": "launchplane",
  "operation": "merge-train-controller-run-once",
  "generated_at": "2026-05-16T00:00:00Z",
  "request": {
    "repository": "example/repo",
    "base_branch": "main",
    "mutate": false
  },
  "summary": {
    "launchplane_status": "accepted",
    "trace_id": "launchplane_req_example",
    "controller_action": "build_candidate",
    "recommendation": "Call the controller again only after reading this action."
  },
  "records": {
    "merge_train_batch_candidate_record_id": "merge-train-batch-candidate-example"
  },
  "result": {
    "repository": "example/repo",
    "base_branch": "main",
    "mode": "dry-run",
    "controller_action": "build_candidate"
  },
  "warnings": []
}
```

Successful responses use operation-specific projections instead of generic
provider dictionary pass-through:

- `merge-train-controller-run-once` may emit only documented controller fields
  such as repository, base branch, mode, mutate, controller action, safe reason
  codes, commit ids, source/workflow URLs, and merge-train record ids. A supplied
  `dry_run_result` also preserves queue order, selected PR, intended next action,
  and bounded next-action detail. Queue entries expose only PR number/head,
  author-role classification, eligibility and refusal reasons, mergeability,
  check status, and branch-update requirement. PR titles, PR label sets, author
  identities, and arbitrary provider fields are omitted. Missing queue fields
  stay absent; they are not projected as an empty or eligible queue. Malformed
  supplied queue fields and unsafe text fail closed.
  An optional `conflict_probe` preserves `null` when no probe was needed, or
  projects its status and probed PR numbers. When supplied, `held_out` entries
  expose only PR number, head SHA, reason code, and the preceding PR numbers
  they conflict with (an empty list means a conflict with the base). Dry-run
  probes can omit `held_out`. Unknown probe or held-out fields, malformed PR
  numbers, and unsafe values fail closed, including on `update_branch` results.
- `product-config-preflight`, `product-config-dry-run`, and
  `product-config-apply` may emit only intent status, reason code,
  safe-to-execute, next action, managed binding keys, runtime key-safety finding
  codes, and product-config/intent record ids. Secret results also show a
  copied secret's `copy_from` (context, instance and version id); any other
  field in it is refused. Schema-v2 runtime retirement
  responses also expose the before/after lists of retired provider key names.
  Each list is bounded, unique, and restricted to uppercase environment key
  names; nonempty lists require instance scope. Nested record metadata is
  validated but not emitted. Older responses may omit these fields. Runtime
  values and unknown response fields remain rejected; this response support
  does not change authorization, private-file, review, or apply requirements.
  Keep these bounds aligned with the service's runtime-retirement contract.
- `merge-train-policy-import-dry-run` and
  `merge-train-policy-import-apply` may emit only active-policy identity and
  digest, candidate record identity, digest, status, target count, replay state,
  and trace metadata.
- `repository-inventory-read`, `repository-inventory-dry-run`, and
  `repository-inventory-apply` may emit only bounded append status, record IDs,
  state/revision/digest metadata, timestamps, history count, request digest,
  and trace metadata.

The projections recognize the current service envelopes, including idempotent
replay metadata, nested merge-train candidate/landing/stack summaries, the
write-intent `record`, and product-config runtime, key-safety, count, and secret
binding metadata. Secret record ids, provider target details, actor identities,
instructions, raw findings, and arbitrary nested dictionaries are not copied.

Unexpected successful provider shapes fail closed as `invalid_response` rather
than being recursively sanitized. Summary fields, including `trace_id`, must be
compact safe identifiers or bounded safe text. Keys or nested payloads that look
like secrets, credentials, cookies, tokens, plaintext values, private keys,
opaque values, raw requests, provider env, or headers are not copied into output.

Unauthorized, unavailable, denied, stale, and mismatched-intent responses keep
the same envelope and include compact `summary.error_code`, `summary.trace_id`,
and `warnings` entries. They do not include raw request bodies.

Public timestamp projections accept UTC timestamps with optional fractional
seconds (up to six digits), preserving their exact value. Policy preflight thus
accepts microsecond timestamps emitted by native policy preparation and retains
that precision in reviewed evidence.

## Admin free text

Every emitted admin `reason` or prose `evidence` uses the shared
`public_operator_text` projection. It normalizes whitespace and redacts
credential assignments (including quoted values), known token forms, long
mixed letter/digit token-like words, and URLs of any scheme. URLs are omitted
because admin prose can name private hosts even without credentials.
Structural fields retain their existing strict validators; invalid types,
empty or oversized text, and other unsafe summary shapes still fail closed.
Apply checks that compare a reviewed reason use this projection on both sides.
The private request keeps the original reason; redaction changes public evidence
only. A projection is lossy and cannot distinguish changes solely inside
redacted spans; service digests and private-payload bindings remain in force
where supported.

## Managed runtime sync

`live-target-runtime-sync-dry-run` and `live-target-runtime-sync-apply` use the
existing `POST /v1/live-target-runtime/apply` local extension. Pass `--product`,
`--context`, and `--instance`; values come from Launchplane's managed records,
never CLI input. The service checks `live_target_runtime.plan` for dry runs and
`.apply` for writes on product/context. The helper's apply command requires both
permissions because it also runs pre-apply and read-back dry runs. It grants
nothing and never switches identity.

Save the dry-run output privately. Apply requires `--reviewed-dry-run`,
`--expected-plan-digest` from `result.plan_sha256`, `--dry-run-evidence-file`, and
`--idempotency-key`. Use a fresh stable key for each later intentional sync;
keep the original key associated with that operation, never with a new sync.
Reconcile through the read-only dry-run command below; do not replay apply
blindly after an uncertain outcome.
The helper binds the review to the lane, provider target
fingerprint and key/count plan, obtains a fresh dry run and refuses a difference
observed in that pre-apply plan before sending apply. It reports changed,
missing, different and retiring key names, counts, provider persistence status and `read_back_matches`; target ids,
provider payloads and values are dropped. After apply it runs another dry run
and requires zero remaining key changes on the same target.

Changes between the pre-apply dry run and the apply can only be detected after
the write by the result comparison and read-back. The digest binds metadata,
not hidden values or managed-record revisions. The service has no reviewed-revision compare-and-swap contract here; a value change
that leaves the same key plan is not detectable by this helper. This is not an
exact-value review guarantee. Provider persistence also does not prove the
running containers received the values: this narrow sync always sends
`deploy: false`, and offers no deploy or restart option.

The global `--timeout` option precedes the subcommand. Its default is 10 seconds
per request; choose a longer timeout when the provider needs more time for the
apply's fetch/update/verification calls, for example `--timeout 60`.

Authorization denials remain denials with their trace. A timeout or unverifiable
response after an apply attempt is `accepted_unverified`, exits nonzero and
requires reconciliation before retrying under any key. Run
`live-target-runtime-sync-dry-run` on the same lane and compare `target_sha256`:
zero `changed_keys` proves current provider persistence. Inspect the retained
service trace and error code to resolve the operation outcome before retrying.
A denied read-back after a successful apply stays `accepted_unverified`; a
failed read-back never becomes successful persistence evidence.

For event-driven generic-web recovery, `deploy_key_sha256` identifies a key but
cannot reconstruct it. Obtain the original deploy coordinates/key from a
service-owned surface or explicit private admin input before using recovery.
This helper does not invent an opaque recovery reference or clear an unknown
provider fence. When the service cannot supply that evidence, track the service
prerequisite and leave recovery held.

## Policy Proposals

`privileged-policy-propose --payload-file <private-envelope>` is a bounded local
extension of `POST /v1/agent/privileged-operations/plans`. Use the normal private
`local_operator` configuration. It accepts only `managed-authz-policy-set` and
`managed-merge-train-policy-import`. The service is the request-schema authority;
see [Launchplane's proposal contract](https://github.com/cbusillo/launchplane/blob/main/docs/privileged-operations.md#agent-policy-proposals).

The private JSON envelope contains `schema_version: 1`, `descriptor_id`, a
stable `source_event_id`, `request` (including reason and optional related issue),
and optional `expires_in_seconds`. Never put it in a repository or pass policy
JSON inline. The helper rejects dedicated preparation contexts and unsupported
request fields. It emits bounded proposal metadata, counts, trace,
and a relative `review_path` on the Launchplane UI host; it never returns
policy selectors or credential-routing fields.

On a timeout or response-verification failure, retain the same envelope and
source event for reconciliation/replay. Never substitute a new identity or a new
source event to force a duplicate proposal. A denial is not a missing token:
report the refused action and trace and follow Launchplane denial handling.
An accepted replay can report an already approved or terminal plan; it does not
renew the approval deadline or perform execution. Check the persisted expiry and
blockers in the UI before requesting approval; a stored `planned` status can lag
expiry reconciliation. Approved or executing plans
need observation, not another approval. Inspect terminal outcomes before an
intentional replacement; an expired, cancelled or revoked proposal needs a fresh
source event for a replacement. The signed-in Director reviews
and approves pending plans in the UI; no helper approval or apply path is added.
