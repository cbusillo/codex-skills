# Launchplane Write-Action Helper Contract

This contract defines the public-safe wrapper for bounded Launchplane admin
actions. It is separate from `launchplane-context.py`: read-only context remains
optional and soft-failing, explicit write actions fail closed, and bounded
admin reads also fail closed when required configuration or authorization is
missing. Command examples run from the `launchplane` skill directory.

For operation authority and local HTTP bindings, read
[the producer contract and caller bindings](agent-operator-contract.md).

The helper lives at `scripts/launchplane-write-action.py`.

Private payload files must be outside the active repository. The helper checks
both the file's location after resolving ancestor directory aliases and its
fully resolved target, comparing ancestor directories by filesystem identity
when their path spellings differ. A repository-local link to an external payload
and an external link to repository-local data are both refused; an external payload,
including an external link to another external file, is accepted.

## Application testing private ingress

`testing-private-ingress-dry-run`, `testing-private-ingress-apply` and
`testing-private-ingress-readback` call the producer's
`prepare_testing_private_ingress` service API. The producer advertises a
`service_api` surface, and this installed caller retains that surface and its
existing Director-agent identity dependency. It adds no permission or provider
fallback. Source contract agreement does not prove deployed compatibility;
confirm normal service delivery before operational use.

Each command requires `--payload-file /absolute/private/request.json`. Its
exact fields are `product`, `context`, `instance` (only `testing`),
`endpoint_key`, `edge_endpoint_key`, `access_list_id`, `certificate_id`,
`operation` (`create` or `disable`) and `reason`. Both numeric IDs must be
positive integers naming existing resources. Obtain real scope and IDs from
service records or scoped admin input. Mode, digest, URL, domain, target and
provider configuration fields are not accepted in this file. The service
derives the hostname from the lane's active private HTTPS health endpoint and
checks application ownership, placement, certificate coverage, access list and
receipt ownership itself. This operation neither deploys nor changes DNS,
access lists, certificates, credentials or production.

Save the dry-run's JSON output in a private file outside the repository. Apply
and readback additionally require `--dry-run-evidence-file`,
`--expected-plan-digest` (the returned `plan_sha256`) and `--reviewed-dry-run`.
Evidence binds the full normalized request including its reason and resource
IDs, the configured service, operation and returned plan. Receipts show the
same product/context/testing instance, endpoint and edge keys, operation and
digests; they omit the reason, numeric resource IDs, URLs and provider topology.
The edge key in the receipt comes from the submitted request, not an echoed
service field. Its verification rests on the service-owned plan digest and
provider readback; the helper does not independently inspect the edge provider.
The helper's binding checks are local review consistency checks; the service
remains authorization and plan authority.

Apply also requires `--idempotency-key`. Retain the exact private request,
saved evidence and original key on an interrupted exchange. The helper never
retries automatically or replans before recovery: a fresh plan would describe
already changed provider state. Explicitly run readback first, then, if still
unverified, resubmit the exact original apply and key for the service's durable
recovery. A denied call stays denied; neither another identity nor a raw
provider route is supported. A successful apply or replay is followed by a
separate `mode=readback` POST with the same reviewed digest and request, without
an idempotency header. Only aligned readback yields `read_back_matches=true`.
Disable requires a fresh review of `operation=disable`; it leaves the
application hostname and bot state intact.

Transport loss or malformed evidence after apply begins returns
`outcome_unknown`; failed readback after an aligned applied response returns
`accepted_unverified` and retains that response's result and trace. Both exit
nonzero. A service result with `applied=false`, `replan_required=true` and
`previous_effects_absent=true` is a terminal original-key recovery outcome,
returned as `replan_required` with nonzero exit and no success/readback claim.
Only that explicit absence proof permits obtaining a fresh reviewed plan and
new key. Other ambiguous outcomes preserve the original request and key.

Neither plan nor provider readback verifies actual TLS, WSS, public denial,
remote tailnet reachability or runtime health. The returned
`deployment_performed=false` and `transport_verified=false` preserve those
limits. Actual qualification belongs to the operational issue after supported
source and caller delivery.

## Disposable lane observations

`disposable-lane-inspect`, `disposable-lane-collect`,
`disposable-lane-record`, `disposable-lane-clear` and
`disposable-lane-prepare-rebuild` call the producer's `record_disposable_lane_safety`
service API with the existing Director-agent identity. They add no permission
or provider fallback. An observation
is short-lived safety evidence for one lane. It is never an access grant.

For the producer's supported modes and requirements, read
[the producer contract and caller bindings](agent-operator-contract.md).

Each command requires `--payload-file /absolute/private/request.json` outside
the repository. Inspect, collect and clear use `product`, `context`, `instance`
and an optional `preview_id`. Collect and record also accept `lifetime_seconds`
(1 to 900, default 300). Record accepts only testing scope and an optional
`replaces_operation_id`; all fact assertions and evidence references are refused
before configuration. Its service receipt reports unknown facts and requests no
preview reconciliation. A previous testing receipt's optional
`reconcile_requested` is accepted only when strictly `false` and is omitted
from output. Preview record is unsupported under
[Q652](https://github.com/cbusillo/launchplane/issues/4434#issuecomment-6341807424):
closed-PR preview teardown uses the preview's own-resource guard, without caller
usage or custody assertions. Inspect and collect remain observation tools.
Instance `prod` and an instance other than `testing` without a `preview_id` are
refused before configuration.

Run inspect first and save its JSON output privately. It returns the lane's
`binding_sha256`, with every fresh fact `unknown`, plus `observation_sha256`
and a nullable `observation` holding the stored report's usage, data and
production-resource values. A response missing either is invalid. Stored
reports do not establish current disposal eligibility or replace the fresh
facts. Collect, record, clear and prepare-rebuild each require
`--expected-binding-sha256` (that value), `--inspect-evidence-file` (the saved
output) and `--reviewed-inspect`. The evidence must match the private request
scope, the configured service, the binding digest, the stored report digest
and its summary. Evidence saved before this proof included the report must be
replaced by running inspect again. The helper never runs inspect
inside a mutation for you. The service rechecks the binding and refuses a
changed lane with a 409.

Clear additionally requires `--expected-observation-sha256` from that saved
inspect. Its private payload accepts only the lane scope. The service compares
both reviewed digests transactionally before removing the observation. A changed
report or lane binding refuses without clearing it. Resolve the reason
for any protective report before clearing it; absence is not proof of non-use,
data custody or isolation. Clear does not delete lane resources, lift holds or
alter historical operation records. There is no clear dry-run mode; the supported
review step is inspect. Its receipt must echo both reviewed digests and
`provider_mutation=false`.

Prepare-rebuild takes only the lane scope and required `replaces_operation_id`
in its private payload. It accepts only instance `testing`, without a
`preview_id`, fact assertions, evidence references or lifetime. The service
requires the operation to belong to that exact lane, be service-owned and held
in `reconciliation_required`, and excludes its executor while capturing the
retained target ID in a separate preparation record. It does not rebuild the
lane, delete resources, resolve holds or attest to historical provider effects.
The receipt must match the requested operation and reviewed binding, return
`historical_effects=unknown` and state `provider_mutation=false`. There is no
dry-run mode or idempotency key; inspect is the supported lane review step.
The record form can carry `replaces_operation_id` for that same testing lane.
The producer resolves the retained target from its preparation record; callers
cannot name a replacement target or assert qualifying facts. The record receipt
and subsequent inspect do not echo or independently verify this operation link.
Recording it does not qualify resources, complete isolation or release a fence.
Use the separate resource caller below and the producer's
`docs/disposable-lanes.md` for the supported workflow.

Output never echoes the product, context, instance, preview ID, evidence
references, replaced operation ID or the observing identity. The request shows only the mode and the
payload source. Collect reports each
fact, the usage diagnostics and, for each evidence reference, only whether it is
present and its source kind. Coverage sources, evidence kinds and usage
diagnostics are closed vocabularies copied from the producer. A usage
diagnostic the helper does not know is reported as `diagnostics_unrecognized`,
as the producer's own client does. On a preview, a fact kept from an earlier
recorded observation reports the kind `recorded`. The producer can retain those
earlier protective reports; this reader permits no new preview assertions.
Testing collection also recognizes the producer's `service:testing-resources`
evidence for reproducible data and `separation_ready` after replacement readback.

A receipt that fails these checks reports no facts and carries an
`invalid_response` warning: a wrong binding, `provider_mutation` other than
`false`, an unknown fact value, unknown coverage sources, an evidence reference
the producer does not build for that fact, a positive fact returned for a
testing record, collect usage
evidence that contradicts itself, or a collect window that is not current. The
window must have started no later than now (5 seconds of clock skew allowed),
must not have expired, and must last no longer than the requested lifetime. For
inspect this is `invalid`. For collect, record, clear and prepare-rebuild the service has already
answered, so it is `outcome_unknown` with the recovery below.

There is no automatic retry and no `Idempotency-Key`. Transport loss, a
malformed receipt or a 5xx after a mutating call begins returns
`outcome_unknown`. Inspect the lane, then re-run the command explicitly if it
is still needed. A 403, 404 or 409 keeps its trace and stops; never change
identity or route. A collect 409 can follow publication, so inspect again
before relying on any stored fact.

After an accepted collect or record, the helper sends one separate inspect
POST. It reports `binding_readback_matches`. `effect_readback` is always
`unavailable`: this readback checks the binding, without matching the stored
report to the submitted effect. A mismatch or
failed readback returns `accepted_unverified` and exits nonzero.

After accepted clear, the separate inspect must confirm the same binding and
an absent observation with its canonical null digest. Only then does the helper
return `accepted` with `effect_readback=verified` and
`observation_readback_matches=true`. A failed readback, changed binding or
new observation returns `accepted_unverified`; an uncertain clear exchange
returns `outcome_unknown`. Neither is retried. A successful nonempty clear
changes the report digest, so replaying its original request is not idempotent.

After accepted prepare-rebuild, one separate inspect checks the lane binding.
`operation_id_matches=true` means the receipt agreed with the private request;
the operation ID is omitted. `effect_readback=unavailable` remains explicit:
inspect cannot read the separate preparation record. A matching binding permits
`accepted`, but does not verify that capture. A failed or changed binding gives
`accepted_unverified`. A lost or malformed prepare exchange gives
`outcome_unknown`. Preserve the original request and operation ID. Re-inspect
the lane; when its binding is unchanged, review and explicitly resubmit the same
preparation request. The service preserves an existing capture instead of
overwriting it. Do not retry automatically. A later refusal stops that attempt
but does not establish whether an earlier capture exists, for example when the
operation has since left `reconciliation_required`. Inspect cannot resolve
that capture uncertainty or prove historical effects absent.

## Read budgets and timeout diagnostics

`dokploy-inventory-read` and `product-promotion-status-read` default to a bounded 30-second HTTP socket
timeout to accommodate complete release-checklist compilation. Other reads,
including the read-only POST used by `target-replacement-plan-read`, keep their
10-second default. An explicit `--timeout` before the subcommand overrides
the budget. Write and dry-run commands retain their existing budgets; the
controller budget is described in [Merge Train Controller](#merge-train-controller).

A direct or URL-wrapped socket timeout returns `unavailable` with
`client_timeout` and `timeout_seconds`. It means the client budget expired,
not that Launchplane is down. No response trace was received; retry the read
with a longer explicit budget. Other connection failures, HTTP refusals,
configuration failures and invalid projections keep their distinct diagnostics.
The helper does not retry automatically.

Interrupted HTTP bodies and disconnected responses produce bounded JSON just
like other transport failures; exception text and partial provider bodies are
never emitted. A failed apply preflight stops before POST. Once an apply POST
has begun, a lost response is `outcome_unknown` and requires read-back before
any retry, retaining the original request and idempotency key. A verified
apply whose accepted response was projected but whose read-back was interrupted
is `accepted_unverified`, with the accepted result retained. Explicit reads and
unavailable applies exit nonzero; optional context still exits `0` with an
unavailable envelope. Client-review reads also fail with bounded JSON and a
nonzero exit.

## Dokploy inventory read

`dokploy-inventory-read --limit 25` reads `GET /v1/dokploy/inventory`
using existing private admin configuration. This bounded helper binding requires
the service's existing global `dokploy_target.inspect` read boundary; it creates
no grant. It includes empty/untracked projects, environments and targets, without
depending on product records. All provider operations behind the service are GETs.

The result preserves project/environment IDs and display names, target IDs,
categories and display names, and placement server IDs/display names. Local
placement is `dokploy_host`, with null server fields: no OS/guest hostname is
inferred. Server display names are provider labels, not independently verified
guest identities or running-workload/health evidence. Output contains no env
values, credentials, addresses or compose source. Unexpected fields fail closed.

Follow each `next_offset` using `--offset N --inventory-digest DIGEST` until it
is null, keeping the first page's digest. `total_projects`, `total_environments`
and `total_targets` cover the provider credential's visible snapshot. The provider
root uses unpaginated `project.all`; Launchplane pages projects in ID order and
hydrates the selected page. The digest binds project/environment names and IDs
and target membership, not a transaction over target detail/placement reads.
On HTTP 409, restart at zero. No failure yields a partial-success page. Reduce
`--limit` on a page-target budget failure; a single project over the service
budget remains unavailable. Runtime use waits for normal delivery of the service
route; this command never falls back to direct provider access.

## Names-only settings and secret reads

```bash
uv run scripts/launchplane-write-action.py context-runtime-settings-read --context example-site
uv run scripts/launchplane-write-action.py instance-runtime-settings-read --context example-site --instance testing
uv run scripts/launchplane-write-action.py context-secrets-read --context example-site
uv run scripts/launchplane-write-action.py instance-secrets-read --context example-site --instance testing
```

These four helper bindings use the existing authenticated GET routes under
`/v1/contexts/{context}` and `/v1/contexts/{context}/instances/{instance}`.
They add no permission, workflow fallback or direct storage access. A denial
retains its error code and trace, with no second request.

`result.settings` contains only each record's scope, context, instance, `keys`
and `withheld_keys`. `result.secrets` contains only name, integration, scope
and status. Unknown fields, values, ciphertext, descriptions and audit metadata
are never projected. Malformed retained metadata fails the read. The service
accepts any non-empty settings key, so a key that is not env-key shaped is left
out and counted in `dropped_name_count`; a secret-looking key fails the read.
The envelope
keeps the requested context/instance and service trace. Both record lists and
each key list are capped at 500; `records_truncated` and `names_truncated`
report the bounds. A truncated or access-filtered list cannot prove absence.

The settings endpoint currently transports non-credential plain values too.
The helper does not access its `values` field and never emits it; this is an
output projection, not a server-side names-only transport option. Do not use a
live settings call when the task forbids receiving values. Secret endpoints
return status metadata, without requesting or decrypting managed values.

Global records returned by the service remain visible in these inventories.
An empty context/instance on an inherited global record is valid. This is a
record inventory, not proof that a global key reaches a site's effective
environment. A product-only binding read cannot replace it when auditing
legacy global-inclusive paths. No resolver, delivery, or global authorization
policy changes here.

## Merge-train enrollment read

`merge-train-policy-read --repo OWNER/REPO` calls the complete active-policy
`GET /v1/work-graph/merge-train/policy` read using existing private admin config
and the service's standing `merge_train.policy_targets` permission. Omitting
`--repo` resolves the origin remote in `--repo-root` (default current directory).
No grant, credential resolution, policy mutation or train entry occurs.
The endpoint has a bounded local helper binding.

On success, `status=available` and `result` contains `source=launchplane`,
`status=enrolled|not_enrolled`, boolean `enabled`, per-branch `targets`
(`baseBranch` and `readyLabel`), policy record id/time/digest, and a trace id.
The projection drops other repositories, authorization and credential source
metadata. Matching ignores repository case; distinct branches remain separate.
Only a complete active-policy response proves absence. Missing config, denial,
transport failure, duplicate targets or malformed policy mean unknown to callers.
The snapshot emits `launchplane.mergeTrain.status=unknown` and `enabled=null`
on those failures, independently of `github.json`, with a compact safe `reason`
code for diagnosis. Before attempting this read, the snapshot checks local
configuration presence under the [snapshot schema](../../github/references/config-schema.md).
Confirmed local absence skips this helper; the GitHub skill's
[Merging](../../github/SKILL.md#merging) procedure still applies the Director's
direction and instruction precedence when choosing a landing route.
A read grants no merge authority.

## Odoo addon settings

`odoo-addon-settings-dry-run` and `odoo-addon-settings-apply` call
`POST /v1/product-config/odoo-addon-settings/apply` to set an Odoo lane's
Shopify addon settings on its instance-override record. The Launchplane
operation is `apply_odoo_addon_settings`, defined by the producer contract.

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

## Odoo website settings

`odoo-website-settings-dry-run` and `odoo-website-settings-apply` call
`POST /v1/product-config/odoo-website-settings/apply` to prepare a product's
production website settings. The Launchplane operation is
`apply_odoo_website_settings`, defined by the producer contract.

- The private payload file holds `product`, `context`, `reason`, optional
  `instance` (only `prod`), and `website_settings`: `artifact_id` naming the
  product's current testing artifact plus `settings` with `website_id`,
  `public_origin` (HTTPS), `public_indexing_enabled`, `google_analytics_key`,
  `cookies_bar` and `block_third_party_domains`. Unknown fields, a non-HTTPS
  origin and collection-only rollback are refused locally. Launchplane checks
  the origin, artifact and analytics consent rules.
- `website_settings: null` withdraws the pending intent.
- Apply uses the same gates as addon settings: `--reviewed-dry-run`,
  `--expected-plan-digest`, `--dry-run-evidence-file` for the same product and
  context, and `--idempotency-key`.
- Preparation writes inert intent and changes no Odoo database. The
  Client-accepted release applies it.

## Integration allowances

`integration-allowances-read`, `integration-allowances-dry-run` and
`integration-allowances-apply` call `GET /v1/product-config/integration-allowances`
and `POST /v1/product-config/integration-allowances/apply`. A non-production lane
may hold an integration's settings only when an allowance recorded here says why.
These routes have installed local helper bindings.

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

## Health monitoring

`health-monitoring-dry-run` and `health-monitoring-apply` use the bounded local
extension `POST /v1/product-profiles/health-monitoring/apply`. The service's
[request contract](https://github.com/cbusillo/launchplane/blob/main/docs/records.md)
owns the exact-instance planning/apply behavior and authorization.

Both commands take `--product`, `--context`, `--instance`, `--check-name`,
`--check-kind public_http|private_http`, `--monitoring-intent public|private|prelaunch`,
`--enabled` or `--no-enabled`, `--require-runtime-identity` or
`--no-require-runtime-identity`, and `--reason`. A private check may also name a
registered `--private-endpoint-key`; enabling a private check requires it.
For a disabled private check an omitted key preserves the existing key.
No argument accepts an endpoint URL, domain, provider target or full profile.

The lane's dependency coverage policy is optional. Omit every dependency flag
to preserve it; the request is then identical to one from before the policy
existed. To change it, pass `--dependency-mode informational|required` with
`--dependency-source derived|override`. The service replaces the whole policy,
so the source is always explicit and changing the mode never drops an existing
override silently. An override takes zero or more `--dependency
database|email|cache` (zero is an explained none) and a
`--dependency-override-reason`; `derived` accepts neither. The dry-run shows the
current and requested mode and override dependencies, and that policy joins the
reviewed request digest, the saved-evidence check on apply and the read-back.
Read-back compares the whole stored policy, reason included, against the
request or, when omitted, against the policy read before apply. The service
still refuses edits that would leave required coverage without an enabled
HTTP check; switch to informational in the same reviewed change.

Save the dry-run JSON outside the repository. Apply requires that evidence via
`--dry-run-evidence-file`, its `plan_sha256` via `--expected-plan-digest`,
`--reviewed-dry-run`, and a stable `--idempotency-key`. A request digest binds
the exact arguments, including the reason and private endpoint key, while the
service binds the reviewed plan to the current profile. The helper reads the
profile before apply and stops if the resolved endpoint differs from review.
After apply it reads the selected lane/check back, compares the intended policy,
and verifies preservation of the stored public URL and lane fallback URLs
privately. Private keys must match the requested or preserved registered key.

Receipts contain the lane/check, intent and flags, dependency mode and override
kinds, digests, trace/record ids and read-back verification. URLs, endpoint keys
and free-text reasons, including override reasons, are omitted.
`accepted_unverified` with a nonzero exit means apply was accepted but the
response or read-back did not verify; inspect the record before any retry.
An uncertain transport outcome retains the existing helper's reconciliation
rules and stable idempotency key. A verified policy is not proof that a monitor
has persisted observations or maintained a cadence. Source delivery does not
authorize live apply or grant the service's plan/apply permissions.

## Testing hold

`testing-hold-read`, `testing-hold-dry-run` and `testing-hold-apply` call
`GET /v1/product-config/testing-hold` and
`POST /v1/product-config/testing-hold/apply`. While site staff test a product's
testing lane, the hold keeps event-driven deploys off it. These routes have installed local helper bindings.

- `testing-hold-read --product --context --instance` returns the lane's hold or
  `null`.
- Dry-run and apply take `--product`, `--context`, `--instance`, `--hold` or
  `--lift`, and `--reason` (the hold's reason, or the audit reason for a lift).
  `--purpose staff_testing|isolation_staging` defaults to `staff_testing`.
  Nothing in the request is secret, so there is no payload file.
- Save the dry-run output. Apply requires `--reviewed-dry-run`,
  `--expected-plan-digest` equal to the saved `plan_sha256`,
  `--dry-run-evidence-file` for the same product, lane, hold direction, purpose
  and reason, and `--idempotency-key`. Retain the original request and key for
  an apply retry. Legacy evidence without purpose means `staff_testing` only;
  staging requires a saved purpose-qualified request, including no-op lifts.
  Staff requests keep the legacy wire body without an explicit purpose, while
  saved request summaries record it; this preserves the server's raw-payload
  idempotency fingerprint for a retry after upgrading the helper.
- Output shows the action, the hold before and after (reason, recorder and
  time, optional purpose and service-derived rehearsal provenance), read-back,
  and whether lifting requested a testing reconcile. Rehearsal provenance is
  closed to binding digest, target update time and target source label; the
  helper does not accept it as write input.
- An isolation-staging lift alone does not request reconciliation or deploy.
  Ordinary staff lifts request testing reconciliation. The service remains
  authoritative for protected holds, qualification, denials and uncertain
  effects; selecting a purpose grants no authority and never relabels an
  existing hold. Continue through the same authenticated helper path.

## Product environment, activity and preview reads

`product-environment-read` and `product-activity-read` call
`GET /v1/products/{product}/environments/{environment}` and
`GET /v1/products/{product}/activity`; `preview-history-read` is below. They let an agent confirm what a
merge-triggered deploy shipped without an admin signing in. These routes are
installed local helper bindings.

- `product-environment-read --product --environment` returns the lane's
  identity, `target.artifact` (artifact id, source commit, image repository and
  digest, source build run), expected and observed runtime identity (deployment
  record id, artifact id, source ref, image reference, deployed time) with the
  runtime identity status, health checks, public ingress status, trust state and
  provenance.
  Monitoring has no summary `trust_state`: `public_incident_eligible` and
  profile provenance describe intent. Each check retains `probe_effective`,
  `incident_eligible`, observation `record_id`, `observed_at`, `trust_state`,
  and bounded provenance including `stale_after`. Consumers assess observation
  freshness from that canonical evidence; newer profile provenance does not
  renew a probe. Absent effectiveness/eligibility fields remain `null`, and
  absent provenance remains `null`, rather than proving a check suppressed or
  fresh. Private endpoint keys, URLs, free-text provenance detail and unknown
  fields are dropped; malformed retained values fail the read. Check output
  remains bounded with `checks_truncated` disclosing omitted checks.
  `health_monitoring.open_incidents` retains incident IDs, check names, status,
  severity, failure code and layer, including provider warnings independent of
  healthy checks. `topology.warnings` retains code, scope and severity;
  `topology.observed.tls_domains` retains DNS names, states and provenance.
  These lists disclose full counts and truncation; omitted legacy lists remain
  `null`, never an empty all-clear. `other_warning_count` counts lane warnings
  whose detail is absent from topology warnings, matching the sidebar's separate
  warning branch without exposing prose. Missing warning inputs remain unknown.
  This read exposes red evidence; it cannot settle the complete amber/green
  state, which also needs the sidebar's placement, public-observation and
  applicability inputs. Verify green with complete canonical sidebar data.
  Route-diagnostic warning codes remain route-change evidence and are excluded
  by the sidebar's `healthWarnings` rule; their severity alone is not a color.
  Passing checks and identity alone never establish green. Preserve unknown
  and truncated results.
  Incident summaries, warning detail, certificate contents, URLs and other
  topology fields are omitted. The helper reports evidence, not a second color
  algorithm or authority to clear an incident.
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
  Empty artifact ids remain valid for preview feedback protecting image references
  only. Source record ids are required and can embed a digest-pinned image reference
  (for example, a release-tuple id); they use the same SHA-256 suffix and userinfo
  checks as artifact ids. Nonempty image digests must be SHA-256, and lane names
  cannot contain `@`. Malformed retained fields fail the read. This diagnostic projection
  is not a complete registry-cleanup retention set when truncated.
- `product-profile-read --product` calls `GET /v1/product-profiles/{product}`
  and returns the product's Client GitHub login and review label, `production_use`,
  `release_on_acceptance`, lifecycle state, display name, driver, repository and
  lanes (context and instance only), plus the optional typed business-hours policy
  with its wire presence preserved. The summary contains service status and trace
  provenance only. For release authority, follow the
  [release rule](../../references/role-words.md#release-rule) and read service-owned
  evidence with `product-promotion-status-read`. `release_on_acceptance` is `held`, `promote`,
  `promote_with_rollback_drill` or `director_standing`, as defined in
  [Launchplane's release review](https://github.com/cbusillo/launchplane/blob/main/docs/release-review.md#acceptance-starts-the-release).
  An omitted field reads as `held`; any other value or type fails the whole
  read with `invalid_response`. A malformed or secret-looking value fails the
  whole read.
- The shared ready-handoff acceptance reader uses the bounded helper
  `repo-product-mapping-read` route internally to resolve a repository's product
  before `product-profile-read`. The dedicated mapping read is GET-only; it is
  not an exposed write-action CLI command. An absent or ambiguous mapping,
  mismatched profile or unavailable read never supplies standing acceptance.
  Any active product classification can supply the recorded Director standing;
  a repository pinned into another Client's Odoo lane still requires Client
  review. Standing reads inspect Odoo prod/testing lane pins; unavailable or
  truncated lane evidence and ambiguous mappings withhold standing. This does
  not change ordinary Client-note discovery when its mapping is unknown.
  All enrolled products still
  require Client test notes, including the existing no-test reason for tooling;
  standing acceptance removes the per-PR acceptance step.
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
  to it the same way they do to `last_error`. A testing plan also keeps
  `current_commit`, its build-selection counts and flags, and `rejected_builds`:
  each newer build that failed verification, by full `commit`, selected positive
  `run_id` (or null when not selected), and the service's fixed-format `code`.
  Free-text `error` is still dropped and counted under
  `requests[].last_plan.rejected_builds[].error`. Malformed identifiers/codes
  are omitted and counted; a rejection without a usable commit is omitted and
  counted as an entry. Lists retain the existing 20-entry bound.
  `pr_feedback` retains bounded `status`, `delivery_status`, `delivery_action`
  and feedback-time `owner_review` codes, `feedback_id`, nonnegative PR/comment ids,
  and the comment body's `body_sha256` digest. Invalid or empty optional fields
  are omitted; malformed values are counted. Nonempty error prose is withheld
  and counted by name, other unknown nested fields under `<unlisted field>`.
  Missing legacy fields stay missing and new code values stay explicit; the
  helper never infers terminal feedback, delivery or current Client acceptance
  from an absent comment, missing status or a historical feedback record.
  The plan also retains `failure_trace_id`, `failed_restore_deployment_record_id`,
  `testing_runtime_drift_sha256`, `testing_deploy_recovery_refusal` and the
  `held_reservations_truncated` flag, with the existing scalar safety rules.
  `destroy_retry_key`, `preview_recovery_plan`, `destroy_retry_stop_reason` and
  `feedback_recovery_stop_reason` are withheld and counted under their own names.
  The reviewed `artifact_scan` object can carry a registry/image reference;
  `held_reservations` and `testing_deploy_recovery` carry original-operation or
  provider evidence. These raw objects are deliberately withheld and counted by
  name; their presence is not absence or a recovery clearance. The plan's
  duplicate `product` is withheld because the result already supplies the
  product scope. The retained action, reason, hold and refusal fields remain
  the decision evidence. Other unknown plan fields are dropped and counted
  under `requests[].last_plan.<unlisted field>`;
  the same drop and omit rules apply.
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
- `testing-rebuild-dry-run --product --copy-source --artifact-id --source-git-ref
  --reason` reads the native preflight through
  `POST /v1/drivers/odoo/target-replacement-plan`. It records no artifact,
  allocation, lease or provider effect. Its bounded receipt identifies the
  testing lane's address origin, build/source, observation time and pending
  candidate verification. It does not attest a healthy successor or completed
  restore. Save the JSON output outside the repository and inspect it.

  `testing-rebuild-submit` with those same intent arguments plus
  `--idempotency-key --reviewed-dry-run --expected-plan-digest
  --dry-run-evidence-file` queues one testing rebuild through
  `POST /v1/drivers/odoo/target-replacement-apply`. It fixes the instance to
  `testing`, strategy to `replace-and-cutover` and health verification on.
  `--copy-source` is `empty`, `production` (sanitized production copy) or
  `upstream` (the lane's configured upstream). The build/source pair must use
  an immutable 40- or 64-character lowercase hexadecimal source commit. A concrete
  reason is required and retained by the service; public helper output carries
  only its request digest. Empty allows
  initialization of independent empty storage. No target, host, credential,
  arbitrary payload or health-bypass argument is accepted.

  Use the saved receipt's `result.plan_sha256` as `--expected-plan-digest`.
  The existing reviewed-evidence loader checks the exact intent, reason and
  returned preflight digest before loading admin configuration. A fingerprint
  also binds the evidence to the configured service before any submit call.
  A changed intent or service needs a new dry run. This is the ordinary
  preflight for one native queued action; it does not allocate/bootstrap/restore
  or repair routing through separate manual writes. The service rechecks current
  authority and copy safety before effects.

  Use the configured service identity and existing service authorization.
  These commands add no grant and never invoke a provider or local live-target
  CLI. They require a deployed service with native testing rebuild and preflight/
  reason support (Launchplane #3997 and #4002 source); a source PR is not activation.
  A 400 can mean the service predates this capability, while 404 means the
  route is absent; confirm deployed support before use. A denial retains its
  code and trace and stops the action. There is no legacy rebuild,
  alternate identity or workflow fallback.

  The receipt must agree with the submitted product, testing instance,
  original key and build/source intent. Output reuses the bounded operation
  projection below, retaining the operation ID and service trace while dropping
  raw authorization, poll URLs and request/provider evidence. Acceptance is not
  completion. Poll `target-replacement-operation-read --operation-id ID` using
  that original ID until the service reports its outcome; a failure or pending
  reconciliation does not authorize a different rebuild.

  The helper sends once. A timeout, unsafe redirect, malformed/misaligned receipt,
  interrupted exchange or server error reports `outcome_unknown`. Keep the
  original key and arguments. Read the original operation if its ID is known;
  an ID is retained only after the receipt agrees with the original key and
  complete intent, even if later output projection fails. A mismatched receipt's
  ID cannot identify this request's outcome. With no aligned ID,
  the service's identical-key submission can recover its existing
  admission receipt. Do not choose a new key or replay a denied action. The
  helper does not retry, poll automatically, rotate secrets or retire storage.
- `target-replacement-operation-read --operation-id` calls
  `GET /v1/drivers/odoo/target-replacement/operations/{operation_id}` with no
  query. The service authorizes it as `operations.read` on product
  `launchplane` for the operation's context and instance, or as
  `odoo_target_replacement_apply.execute` on the operation's product, context
  and instance. The read grant returns a structured view without the request
  or embedded result; the execute grant returns the full service record. The
  helper projects either view and a supplied top-level `recovery` into
  `result.recovery` using
  Launchplane's service-owned
  [TestingReplacementRecoveryRead](https://github.com/cbusillo/launchplane/blob/main/control_plane/contracts/testing_replacement_recovery.py)
  schema. Only its closed codes, nonnegative effect count and strict booleans
  are accepted; unknown fields, missing required fields or malformed values
  fail the read. Nullable attempt/binding agreement and effect acknowledgement
  fields normalize omitted values to `null` (unknown). Absent or null recovery
  remains unavailable, never eligible. `safe_to_release` must remain false:
  this observation does not release the fence, and zero journaled effects or
  an absent journal cannot establish absent provider effects.
  The operation projection returns the operation's id, product,
  context, instance, status, phase, attempt, deployment record id, the
  requested artifact id, created, updated, started, heartbeat and finished
  times, `error_code`, a bounded `error_description` using the response-prose
  redaction policy,
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
  instance. Optional `--artifact-id` and `--source-git-ref`, given both or
  neither and each a public identifier, are sent as `replacement.artifact_id`
  and `replacement.source_git_ref` and echoed in `request`; the service then
  evaluates that candidate artifact instead of the lane's current one, without
  accepting a release or changing inventory. A partial or unsafe pair is refused
  before a request is sent. It returns `plan_status`, product, context,
  instance, strategy, `expected_artifact_id` and `expected_source_git_ref` (the
  artifact the plan evaluated), data source mode, the target, target-id
  and inventory found flags, `allow_empty_data`, `retired_provider_keys`,
  `delivered_runtime_keys` (`null` when the service does not report the list),
  `blocker_codes`, `blocker_keys` by code, `blocker_count`, `warning_count`,
  the current target's `env_keys`, `required_volume_keys_missing` and
  `latest_deployment_status`, and each
  step's `step_id` and `status`. Every key list keeps names that match
  `[A-Za-z_][A-Za-z0-9_]*` only. Blocker, step and warning text, the next
  target's name and domains, the approval issue URL, and the current target's
  id, name, domains and live volume values are dropped and listed in
  `dropped_field_paths`, as are names that are not env keys. A secret-looking
  value in a kept field fails the read.
  Provider deployment status is a bounded code from the latest reported
  deployment; omitted, null or whitespace-only status stays `null` (unknown).
  Malformed status is dropped to `null` with its path recorded, retaining the
  other plan evidence. Recognized credential shapes refuse the read. This is
  separate from plan readiness and does not prove an idle source, a complete deployment inventory
  or exclusion of concurrent maintenance. Read the source's current operation
  and deployment evidence before testing restore; no source apply is available
  through this command.
- Path segments must be plain identifiers; anything else is refused before a
  request is sent. Runtime settings, managed secrets, available actions, URLs,
  provider target names and driver extensions are dropped from the output.

## Empty testing replacement resources

`testing-replacement-resources-dry-run`, `-apply`, `-readback` and `-release` call
`POST /v1/products/{product}/testing-replacement-resources` using the existing
authenticated transport. Their private `--payload-file` must be outside the
repository and contain `product`, `context`, `instance: "testing"` and `reason`.
Retained replacements also require their `operation_id`. Optional `source_mode` is `ssh` (default, omitted
on the wire) or `container-export`. The caller accepts no preview, production
destination, volume, credential, domain or placement input. For initial generation,
set `initial_generation: true` and omit `operation_id` or leave it empty; the
service derives the initial intent from the original stable key. An explicit
operation must still match. Supply `--idempotency-key` in every initial mode,
including dry-run and readback. The saved review binds that key within the private-intent digest and the returned
operation ID, which is validated and returned for bootstrap continuation. Keep the same private request
through planning, apply, readback and release.

The dry run records preparation metadata and allocates no provider resources.
Retained replacements exclude the retained executor; initial preparation does
not reserve an executor. It returns the service's
`plan_sha256`. Save its output privately and review it. Apply requires that
output as `--dry-run-evidence-file`, `--expected-plan-digest`,
`--reviewed-dry-run`, and the original stable `--idempotency-key`. Every private
request field and the configured service are bound to the review. Apply sends
the reviewed digest directly without replanning. The service owns its allocation
namespace, retained-executor exclusion, authority comparisons and original-key
provider recovery. The caller neither grants access nor calls a provider.

Initial `-release` requires the same review arguments and original key as apply.
Its exact receipt must match the reviewed operation and lane, confirm
`released: true`, `provider_effects_started: false` and preservation of the
retained target. It does not allocate or qualify resources. An apply replay
that reports the reviewed initial intent as released returns `released`,
`resources_verified: false` and a nonzero exit, without allocation readback.
An uncertain release preserves the intent, review, original key and fence;
inspect effect evidence before another explicit original-key release. Never
clear a fence or delete resources to resolve uncertainty.

Receipts must match the exact product, context, testing instance and reviewed
operation. The plan requires preserved retained target, fresh storage, no host
ports or host SSH mounts and no restore. Apply must echo the reviewed digest and
provide a verified resource binding. A separate actual readback must return that
same binding, preserved retained target, `separation_ready`, and no restore.
Only then does apply return `accepted` with `resource_readback_matches=true`.
Failed or changed readback returns `accepted_unverified`, exits nonzero and
leaves that match false. Standalone readback needs the same private request and
does not require an earlier plan or repeat creation; it can publish fresh
qualification evidence. None of these receipts proves completed isolation,
bootstrap, healthy web, supersession, absent historical effects or fence release.

No command retries itself. Planning loss can leave preparation metadata and
the retained executor excluded;
inspect before explicitly planning again. Lost or malformed apply exchanges and
uncertain 409s preserve the exact request, review, operation and key as
`outcome_unknown`; inspect/read back before supported original-key recovery.
A standalone readback 409 is unverified qualification, also `outcome_unknown`,
and never an instruction to allocate again. After accepted apply with failed
readback, retain `accepted_unverified` and use the separate readback command,
without repeating apply. Both available traces and projection diagnostics survive.

The producer's explicit `testing_resources_refused` on dry-run/apply is a
pre-provider refusal: inspect the current lane and prepare/review a new dry run.
Preserve earlier artifacts and any original key. This refusal says nothing about
earlier attempts, and retained preparation can still require its earlier digest;
if a fresh review is refused again, stop and report the trace with the retained
operation instead of cycling plans, keys or clearing preparation. Denials retain
the established authority stop/report/no-fallback guidance and the current
apply request, review and key,
including when readback is denied after accepted apply. Other definite refusals
keep their service code, trace and corrective guidance. No refusal proves that
an earlier attempt had no effects. Never allocate a second target or delete
partial resources to recover. The ordinary `--timeout` override remains available.

Public output contains only modes, matching flags, safety booleans, qualification
and review digests, and bounded service diagnostics. Product, context, operation,
reason and schedule identity remain private, except the validated initial
preparation operation ID required by bootstrap. Scope and service fingerprints bind
the review without exposing those values. Runtime qualification remains with the
owning operational issue after source delivery.

## Product repository identity

`product-repository-identity-dry-run` and `product-repository-identity-apply`
call `POST /v1/product-profiles/repository-identity/apply`. The service copies
`repository_id` and `repository_owner_id` into the product profile from the
current tracked repository inventory record; the caller never supplies ids, and a
recorded identity is never overwritten. This route has a local helper binding.

- Both commands take `--product` and `--reason`; there is no payload file.
- Save the dry-run output. Apply requires `--reviewed-dry-run`,
  `--expected-plan-digest` equal to the saved `plan_sha256`,
  `--dry-run-evidence-file` for the same product, and `--idempotency-key`.
- Output shows the repository, identity before and after (decimal ids only), the
  inventory record, revision and digest, and read-back.

## Setting up a lane for promotion

These commands record what a product needs before Launchplane can promote it
from a testing lane: its Client, the lane's provider target and lane record, and
the production backup policy. Their routes have local helper bindings. Every apply follows the same safeguards:

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
  `server_id`, `reason`, `healthcheck_path` (starting `/`), a `domains`
  list (empty when no web route is requested), and `project_id`, `project_name`
  or `environment_id`.
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

`dokploy-target-create-application-dry-run` and
`dokploy-target-create-application-apply` use the same setup route with
`operation: "create-application"`: a new project, environment and empty
application for the `testing` lane, with no domain route. Authorization is
the same as compose setup; first testing setup also admits the Director agent
on a lane with exclusive context ownership. It may replace only a
never-deployed target Launchplane created empty, named exactly in
`expected_current_provider_target`.

- The private payload requires `context`, `instance` (exactly `testing`),
  `target_name`, `server_id`, `project_name`, `environment_name` and `reason`.
  Optional: `schema_version`, `project_description`, `environment_description`,
  `description`, `source_git_ref`, `healthcheck_path` (starting `/`),
  `deploy_timeout_seconds`, `state_volume_path` (the product's absolute
  container state directory) and `expected_current_provider_target`. Ids,
  domains, ports, git source and `app_name` are refused before a request.
- Without `state_volume_path`, output shows the plan actions for project,
  environment and application. With it, the dry run saves a service storage
  plan; output shows its `plan_id` and `service_plan_sha256` and drops the
  generated project, environment, application and volume names. `plan_sha256`
  covers the payload digest and that plan.
- Apply sends the saved `state_volume_plan_id` and `expected_plan_sha256` from
  the reviewed evidence, never a new dry run, which would allocate new names.
  Keep the first apply's `--idempotency-key` with the evidence: the service
  binds the allocation to it, and a retry or recovery uses that key only.
- HTTP 202 is a receipt. Success needs `applied: true`, a created target, and
  with storage `allocation_state: adopted` and `configuration_evidence:
  matches`. A `retained_uncertain` receipt or unverified mount is reported as
  `accepted_unverified` with a warning. Refusals carry the service's
  `application_state_volume_*` codes; act on the code, not the message.
- Apply reads the target back through inspect and checks the provider-target
  record, that every record names the created application, no domains and the
  reviewed health-check path. Creation deploys nothing; image delivery follows
  the ordinary testing deploy.
- `dokploy-target-create-application-readback --payload-file P
  --idempotency-key K --expected-plan-digest D --dry-run-evidence-file E` reads
  back an uncertain storage allocation. Use the unchanged payload, the saved
  dry run and its `plan_sha256`, and the original apply's key; the helper sends
  the saved plan id and digest with `mode: readback` and no confirmation.
  Readback never creates or adopts. Output shows `operation_state`,
  `allocation_state`, `configuration_matches`, `configuration_evidence`
  (`matches`, `differs` or `unavailable`), `custody_sha256`, and the durable
  `provider_effect_phase` as step names only (`created`, `pending`,
  `mount_verified`); generated names and provider ids drop. A wrong key is
  refused by the service. Save this output as readback evidence.
- When the allocation cannot resume, `dokploy-target-create-application-retain`
  takes the apply arguments under the original key plus
  `--readback-evidence-file` (that readback's output) and
  `--expected-custody-digest` (its `custody_sha256`). Before sending, the helper
  requires that the readback used the same payload, plan, review digest and key,
  and that its custody matches. It sends `retain_state_volume_allocation: true`
  and `expected_state_volume_custody_sha256` with the setup confirmation.
  Success is a `retained_uncertain`, `applied: false` receipt naming this plan,
  key and custody. The retained allocation never resumes; keep its resources
  and start new work from a fresh dry run with a new key.
- For an `application_state_volume_*` refusal, the helper's recommendation is
  chosen by the code, never from the message: wait out an active lease, retry
  a busy lane, read back again after custody changed, apply the original key
  when already adopted, resolve changed ownership, or fix an unqualified request.

### Inert domain and ingress planning

These commands have bounded local helper bindings. Neither accepts an apply mode, confirmation, credential
argument, provider URL or alternate identity. They use the existing configured
local-admin transport. A plan is review evidence for the separate repair commands
below; it grants no service permission or live-repair authorization. Do not run
these as part of source validation; use offline behavioral tests.

`dokploy-target-repair-domain-authority-dry-run --context C --instance I --reason R`
reads `dokploy-target-inspect` internally, then calls the setup route with
`operation: repair-domain-authority`, fixed `mode: dry-run`, and service product
`launchplane`, authorizing `dokploy_target.repair_domain_authority.plan`.
It requires consistent tracked, provider-target and live target
identities, derives the requested domains from the live inspection, and sends
the expected deployed-provider projection privately. The service rechecks the
projection and the exact live domain set. Output verifies those agreements and
`applied: false`, retaining scope, counts, trace and a private-material review
digest with helper apply eligibility. The tracked domain set
can differ: correcting that authority is what this planner describes.
Complete canonical/empty `provider_evidence` is carried privately. For hidden
noncanonical evidence, inspection supplies `comparison_fingerprint`; the helper
validates it and sends `expected_current_provider_target_fingerprint`, then
requires the same fingerprint and visible target identity in the plan response.
The service compares its lane-bound keyed commitment to the entire current
reference and returns no arbitrary evidence. Fingerprints and evidence stay out
of public receipts. Missing comparison input keeps the precise service
prerequisite and trace. Drift or key rotation requires fresh inspection.
Fingerprint plans are not apply-eligible: this capability is dry-run only, and
the separate apply path still requires the complete private reference and all
existing gates. Existing canonical/empty plans retain their apply eligibility.

`ingress-route-dry-run --payload-file <private-file>` takes an explicit local
JSON file outside the repository (no stdin) with only `product`, `context`,
`instance`, `expected_host_id`, `route` and nonempty `reason`. The host id must
identify the observed existing host; never infer it from checked-in examples.
The route requires unique lowercase DNS `domain_names` (1–100) and an existing
`edge_endpoint_key`. Optional scalar fields mirror the observed ingress route:
`certificate_id` (existing nonnegative integer only), `access_list_id`,
`enabled`, `ssl_forced`, `hsts_enabled`, `hsts_subdomains`,
`trust_forwarded_proto`, `http2_support`, `npmplus_http3_support`,
`npmplus_noindex`, `npmplus_crowdsec_appsec`, `npmplus_proxy_request_buffering`,
`npmplus_proxy_response_buffering`, `npmplus_upstream_compression`,
`npmplus_fancyindex`, `npmplus_x_frame_options` and `npmplus_auth_request`.
Other fields, upstream addresses, new certificates, advanced config and
locations are refused. Omitted options use the service defaults; supply the
observed options when those differ. Unsupported route shapes remain a
capability gap, never an invitation to apply or hand-build a raw request.

The service authorizes `ingress_route.plan`. Before posting, internal reads
fetch the fresh product profile and exact active
edge-endpoint record. Domains must equal the lane's public HTTPS hosts from its
base URL, health URL and enabled public checks. The helper sends the endpoint
key, leaving upstream resolution to the service, and forces
`require_exact_expected_host_domains: true`, `allow_create: false`,
`allow_update: false` and `allow_enable_disable: false`. The service's fresh
provider plan must return `dry_run: true`, `status: unchanged`, only no-op
operations, the requested host id and exact domain set. Its comparison must
also report `requested_domains` and the same full provider domain count:
an instance-scoped response hiding extra shared-host aliases is refused.
No existing route binding or audit is required; this supports planning the
missing-authority case. The service records the dry run's own audit evidence;
the helper retains that record id as proof metadata.

Output uses the existing envelope, retaining bounded status, scope, counts,
agreement booleans, the ingress audit record id and service trace. Domains,
host and target ids, endpoint keys, upstream configuration, certificate refs,
provider prose and reasons are dropped. HTTP refusal preserves the service
error code and trace with no retry or identity change. Malformed or changing
plan evidence returns an invalid response with its validated trace.

### Reviewed route-authority record repairs

These are bounded helper bindings using the same configured identity and
existing service APIs. Source delivery grants no execution authority. A service
denial stops the operation; preserve its action, requested scope and trace on
the source and owning authorization issues. Never substitute a workflow,
credential or provider write. Source validation uses offline tests only.

- `route-authority-discover --product P --context C --instance I` confirms the
  exact tuple in the scoped product profile and reads its binding state.
- `route-binding-read` with the same scope returns present/absent and the
  service's current record digest. A structured `404 not_found` from the
  exact current-binding GET needs an empty, exactly scoped binding collection
  read to establish absence; generic missing routes cannot prove it.
- `dokploy-target-repair-domain-authority-apply --context C --instance I --reason R`
  applies the exact host set reviewed with the existing domain dry run. That
  dry run now includes `plan_sha256` and `apply_eligible`. A newly observed alias
  changes the digest and needs a new review and authorization covering it.
- `ingress-route-authority-apply --payload-file <private-file>` records the
  existing ingress no-op reviewed with `ingress-route-dry-run`. The payload has
  the same restricted shape; create, update and enable/disable remain false.
  The full host domain set, active edge record, resolved upstream, route options
  and existing certificate evidence are bound to the review. Missing TLS
  evidence fails closed. The applied audit is read back by exact record id.
- `route-binding-dry-run --product P --context C --instance I --reason R` plans
  `POST /v1/route-bindings/reconcile` after profile and current-binding reads.
  `route-binding-apply` uses the same scope and reason. Coordinates come only
  from the service's candidate, never caller-supplied domains or provider ids.
  Blocked/conflict plans are diagnostic evidence and never apply eligible.

Each apply requires `--reviewed-dry-run`, `--expected-plan-digest`,
`--dry-run-evidence-file` (saved helper JSON outside repositories), and a stable
`--idempotency-key`. Before applying, the helper repeats fresh discovery and
the service dry run, then compares the review digest. Plan material stays
private; only its digest, scope, counts, safe finding codes and trace are emitted.
Legacy dry-run receipts without a digest are not apply eligible.

Domain repair preserves unrelated tracked metadata and verifies the exact
live-domain and target projections after apply. The service compares provider
authority and performs its record write with CAS. Ingress apply must still be
an exact provider no-op; the full apply response verifies host identity and
certificate evidence. Readback verifies the applied audit's scope, domains,
edge, TLS ownership, operations and idempotency key. The service's limited audit
read view is sufficient after that full-response check; host/certificate fields
are also compared when available. Output retains the audit id and both traces
on an idempotent replay. Audit GET authorization currently checks
`ingress_route.plan` or `route_binding.read` at context scope; an identity with
only instance-restricted rules can apply but cannot read that audit. Such a
refusal remains `accepted_unverified` with its apply trace/audit id; record it
on the owning authorization issue and do not broaden grants or switch identity.
Route-binding
plans bind current-record CAS evidence and the candidate's source ids/versions,
domains and route semantics. Only generated refresh times and candidate hashes
are excluded from semantic review, since planning regenerates them. Apply then
compares the service's candidate and reads back its exact record digest.

The helper's reviewed-plan comparison is a client-side preflight, not a
server-enforced reviewed-source lock. The service replans at apply and enforces
its own current-binding CAS; a concurrent source change can produce an accepted
but unverified result, which the helper reports without retry. A lost apply
response is `outcome_unknown`; an accepted response with failed projection or
readback is `accepted_unverified`. Preserve the request/key and resolve the
record state before another attempt. A repeated ingress request can verify a
service replay under the original key. Domain/binding evidence normally changes
after a successful write, so repeating the old review returns `stale`; that can
mean the prior write succeeded and requires current-state inspection, never a
fresh key to force another write. No command retries or changes identity.
An apply-time blocked/conflict binding plan is `stale` with `applied: false`,
because the service returns it before mutation. An unresolved mutation lease
or reconciliation-required response stays `outcome_unknown` with its specific
service code, rather than implying a harmless plan conflict.

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
command for it. It needs the provider target first. Public/private lane
requirements belong to Launchplane's
[stable-lane repair implementation](https://github.com/cbusillo/launchplane/blob/main/control_plane/product_stable_lane_repair.py),
which validates the stored product declaration and tracked target; do not
fabricate a domain for a private product. Qualify the currently pinned worker
against the requested shape before dispatch: backend support alone does not
prove that an older protected worker accepts it. Preserve that worker's
authorization binding; a rejected shape is a capability gap, not permission
to repin or call a raw route.

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
  dropped. Optional scoped CA material is validated and replaced with
  `tls_ca_sha256`; certificate contents are never printed.
- The private payload file holds `endpoint_key`, `product`, `context`,
  `instance` and `url`, with optional `status` (default `active`),
  `source_label`, `schema_version` and `tls_ca_pem`. Nonempty CA input is
  limited to testing HTTPS endpoints, at most 65,536 characters, and PEM CA
  certificates with Basic Constraints. Leaf certificates, missing constraints,
  private keys, mixed material and malformed bundles are refused before HTTP.
  The same validation applies to service readback. Empty or omitted CA input
  retains ordinary certificate verification. Launchplane refuses a public URL. The
  helper sets `updated_at`; a payload that carries it is refused. `--reason` is
  required on both dry-run and apply and is part of what the review binds.
- CA bytes are preserved, including PEM line endings, so `tls_ca_sha256` can
  be compared directly with the reviewed public bundle. Run through
  `uv run scripts/launchplane-write-action.py` to supply its declared parser
  dependency; an importer handling CA records needs that dependency in its own
  environment. Missing parser support returns a bounded refusal before HTTP.
- Launchplane does not bind the apply to the dry-run. The helper digests the
  payload and reason with the planned key and scope into `plan_sha256`; pass
  it as `--expected-plan-digest`. It refuses an apply whose payload or reason
  differs from the reviewed one, and sends the reviewed dry-run's `updated_at`
  so a retry with the same idempotency key replays instead of conflicting.
- After apply the helper compares the applied record, and the record read back
  from `GET /v1/private-health-endpoints/records/{endpoint_key}`, with the
  reviewed key, scope, status, URL and scoped CA bytes. Output reports
  `url_matches_review` and `tls_ca_matches_review`, with only a digest for
  nonempty CA material. Replacing or dropping reviewed CA material cannot
  produce a verified apply. This scopes trust to one endpoint; it does not
  change shared monitor trust or establish live TLS/runtime health.

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

Success requires an envelope with `status: "accepted"` and `result.mode`
matching the requested `apply` or `dry-run` mode. Missing or mismatched modes
and non-success statuses fail verification.

If an apply receives an HTTP-success response whose body cannot be parsed,
safely projected, or verified against those requirements, it exits 1 with
`status: "accepted_unverified"`: the comment may
already have changed. Safe available HTTP status, trace and projection-error
diagnostics remain in the summary. Retain the original request and idempotency
key, then run `preview-feedback-remediation --mode dry-run` for the same target
to read back the comment state. A present comment does not prove whether a
replacement completed: the dry run can still plan replacement after a successful
apply. Inspect available trace or service records and resolve the response verification failure
before any apply retry; replaying the same key cannot repair a deterministic
projection mismatch. An invalid dry-run response remains `invalid` and exits 1.

The projected `comment_author_login` and `github_actor_login` accept a bounded
GitHub login with an optional exact `[bot]` suffix for App actors. Brackets
remain invalid in generic identifiers. Observation markers, excerpts and token
actor ids are not emitted; service actor/marker ownership checks still decide
whether remediation is permitted.

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

## Response prose

Output-only prose is redacted at every nesting level. Person-written metadata
also redacts long opaque words; service diagnostics preserve long identifiers
subject to credential-marker redaction. Credential assignments, tokens and URLs are redacted, and
remaining denylist markers hide the entire field. Types, lengths, structural
allowlists and raw secret-shape checks retain their existing validation.
The context helper uses the same service-prose projection.

Reasons compared with saved reviewed evidence (testing-hold, product Client
and image repository), merge-train `ineligible_reasons` matched by downstream
code, and structural fields retain strict validation. Lossy prose never
establishes apply authority or substitutes for machine-matched protocol text.
If an allowance apply receives HTTP success but its response cannot be
projected, its result is `accepted_unverified`, with HTTP status, safe trace
and a readback recommendation. It must not be retried as a failed write.

## Exit Behavior

- `0`: Launchplane accepted the request and the helper emitted a redacted
  summary. `status: "accepted_unverified"` exits 0 for the Odoo addon-settings,
  Odoo website-settings, integration-allowances, testing-hold,
  product-repository-identity, generic-web deploy-recovery, repository-inventory,
  product expected-config, and merge-train policy import applies; product-config
  apply emits it only for the reviewed business-hours path, exiting 1. It exits 1 for the reviewed lane-setup
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
without accepting plaintext values. For business-hours policy, use the standalone
inspection flow below instead:

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

### Public hostnames

The private payload may set `public_hosts` for an exact owned prod compose lane.
Use the same product-config dry-run/apply commands above. Launchplane's
[product-config reference](https://github.com/cbusillo/launchplane/blob/main/docs/service-boundary.md#public-hostnames-on-the-prod-compose-target)
owns hostname semantics, the current-target/provider dry-run requirement,
additive reconciliation, and explicit managed-name removals. Omission preserves
the configuration; `[]` clears only the managed public names.

The optional result `public_hosts` projects hostname lists `before`, `after`,
`added`, `updated`, `removed`, `unchanged`, `read_back_hosts`, plus `plan_digest`,
`runtime_port`, `https`, `service_name`, `certificate_type`, and `verified`.
The helper also reports `resolved_base_url`, derived from the first validated
`after` host under
[Launchplane's public-base-URL rule](https://github.com/cbusillo/launchplane/blob/main/control_plane/contracts/public_hosts.py).
It is `""` for an explicitly empty host list; absent `public_hosts` evidence
omits the whole block. No origin URL or arbitrary response URL is copied.
This is intended configuration in a dry run and verified host configuration in
an apply read-back, not proof that a deployment has rendered the URL. The generic
target-replacement plan's `base_url` remains redacted without public-host provenance.
Unknown fields, non-hostname values, malformed digests/ports, and inconsistent
verification are refused. Existing responses without this result remain valid.
Apply's verified read-back proves provider route configuration, not public HTTP
reachability. No target-setup grant, DNS/TLS change, Client release or separate
target-inspection permission is needed to consume this result. On a stale plan
or partial provider outcome, save a new dry-run and review it before retrying;
never infer success from the previous request's transport outcome.

### Odoo business-hours policy

The same `product-config-dry-run` and `product-config-apply` commands support a
standalone `odoo_business_hours` section on an exact Odoo prod lane. Reuse the
existing service route and current identity. Task scope still owns authority;
this capability does not grant configuration or release permission.

Use a private payload with product, context, `instance: "prod"`, reason and an
optional source label. The calendar section selects `operation: "inspect"`,
`"set"` or `"clear"`. Inspect is dry-run only; it returns bounded permitted-company
references and safe private labels. An optional company ID narrows inspection.
Set requires an explicit company ID and its observed `expected_calendar_id`.
An optional `private_endpoint_key` references an existing endpoint; URLs and
credentials are not accepted. Clear removes the selection. Runtime settings,
secrets, binding removals and public-host changes cannot be mixed into this
request. Odoo remains the timetable authority; no hours or calendar ID persist
in the stored policy.

Save the complete dry-run output. Set/clear apply additionally requires
`--expected-plan-digest` from `result.odoo_business_hours.plan_digest` and
`--dry-run-evidence-file`, alongside `--reviewed-dry-run` and the original
`--idempotency-key`. The helper binds the exact request, reason, source label,
scope and selection to that saved review and supplies `reviewed_plan_sha256`;
do not put that field in the private payload. A new runtime/endpoint authority
plan must be reviewed explicitly even when the chosen company stays the same.

The calendar result distinguishes current runtime qualification from persisted
policy. An accepted apply that matches the reviewed plan performs an independent
profile GET and reports `read_back` and
`read_back_matches`. The policy contains only company ID and endpoint reference.
`product-profile-read` preserves a present `odoo_business_hours: null`, a typed
policy, or omission as separate wire states. The producer may omit its unset
optional default. The apply readback accepts that default only from a matching,
complete typed profile response; missing/malformed/failed reads remain unknown.
No explicit-null observation is invented from omission.

Unsafe company labels are omitted and marked `label_omitted`; keep inspection
labels in private evidence. Unknown calendar/policy fields, malformed identities/digests and
inconsistent scope or selection fail projection. The result's company scope is
the permitted-company set, bounded to 128 companies, not database-wide visibility.
Runtime identity projects only the required lane, deployment, artifact and
source references; other runtime metadata is omitted.
Window `next_release_at` is normalized to UTC. The producer's release prose is
omitted from configuration output; use the observed closing time.

A calendar qualification failure retains its service code and trace and calls
for inspecting/correcting company, calendar, endpoint and current authority.
The helper never retries automatically. An interrupted apply is
`outcome_unknown`; an unreadable or mismatched accepted result/readback is
`accepted_unverified`, both exit 1. Read the stored policy and retain the original
payload, saved review and idempotency key before deciding recovery. Neither
state proves no write. A committed clear receipt and a later profile read are
separate evidence. Source delivery does not configure a product, activate a
live release policy or prove a release drill.

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

For refused or unsupported operations, follow
[Blocked Work Continuation](../SKILL.md#blocked-work-continuation).

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

For an `authorization_denied` response, follow
[Blocked Work Continuation](../SKILL.md#blocked-work-continuation).

## Generic-Web Deploy Recovery

Generic-web deploy recovery is a bounded admin operation for recovering a
generic-web product instance from a failed deploy. For an event-driven testing
deploy, first obtain the exact held reservation from the service:

```sh
uv run scripts/launchplane-write-action.py \
  generic-web-deploy-recovery-reference-read --product <product>
```

This read returns only product/context/testing identity, reservation state and
attempt, and an `event-deploy-<sha256>` reference. Missing, ambiguous, changed,
cross-lane or unauthorized evidence remains a service refusal. It never returns
the original deploy request or key.

Keep a private JSON payload outside the repository containing only `product`,
`instance: "testing"`, `recovery_reference` from that read, and `reason`. Use
`generic-web-deploy-recovery-dry-run --payload-file <private-file>` and save its
output privately. Then, only when apply is authorized, use
`generic-web-deploy-recovery-apply --payload-file <same-private-file>
--reviewed-dry-run --expected-recovery-digest <digest>
--dry-run-evidence-file <saved-output>`. Omit `--idempotency-key`: Launchplane
resolves the stored original key internally. The helper refuses caller keys and
mixed reference/original-deploy payloads, binds the saved review to the exact
reference and reason by `recovery_request_sha256`, and verifies response lane
identity. A changed reference requires a fresh read and review, never guessing.
The service rechecks the reservation and provider evidence against the reviewed
digest on apply. The helper compares the apply reply's action and recovery digest
with the saved review; a mismatch is `accepted_unverified`. A reference does
not grant recovery authority.

Reference recovery also accepts the service's `close_out_observed` plan when
the outcome is `absent` and `retry_safe` is false: the provider operation's
deployment record is absent, while separate runtime evidence proves the exact
running artifact. It closes the reservation without retrying it. Testing
recovery accepts `settle_testing_failure` only with `present` and
`retry_safe: false`. Reference recovery accepts `abandon_testing_original`
only for testing with `unknown` and `retry_safe: false`. This archives the
unknown original without claiming fixture cleanup or qualification. Re-run the
same reference dry-run for abandonment readback: require `reservation_state:
completed`, `proposed_action: replay_completed`, and the saved `recovery_digest`.
A lost apply reply retains the request and review for that readback. All other unknown or uninspected outcomes stay held.
A denied runtime sync remains denied; obtaining
a recovery reference does not deliver runtime values or enable a deployment.
For reference `adopt_observed`, the outcome must be `present`; it need not be retry-safe
because adoption does not retry. `retry_original_operation` requires `absent`
and `retry_safe: true`.

For an explicitly supplied original deploy, supply the private payload
only as explicit admin input in a JSON file outside the active repository or
worktree. The file contains `schema_version`, `product`, `instance`,
`original_deploy`, and `reason`. The apply request additionally requires
`expected_recovery_digest`, which the helper inserts from the `--expected-recovery-digest`
flag after verifying it against any value already in the payload and the saved
redacted dry-run evidence.

With `original_deploy`, both dry-run and apply require `--idempotency-key`. It must be
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

For original-deploy apply, the admin asserts that the private payload is the one reviewed
during dry-run. The helper requires:

- A non-empty `reason` embedded in the payload.
- Explicit `--reviewed-dry-run` acknowledgement.
- The `recovery_digest` emitted by the dry-run result, supplied as
  `--expected-recovery-digest` (64 lowercase hex characters).
- The saved redacted helper output from that dry-run, supplied as
  `--dry-run-evidence-file` from outside the active repository or worktree.
- A stable idempotency key (the original deploy key, used for both calls).

For original-deploy payloads, the helper refuses to send apply unless the evidence is the successful
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
helper binding; it writes nothing and grants no authority.

For Odoo operation prerequisites, select `--path testing --intent
provision|generation|testing-replacement`. Optional `--source-mode ssh|container-export`,
`--operation-id` and `--task-id` select the exact copy source, original resource
intent and provision task; these require an intent. The ordinary deployment read
is unchanged. The service must echo the requested intent; an older service that
ignores it fails response validation. Ordinary service responses omit `intent`
entirely; a null field is not accepted. Validated source/operation/task selectors
are retained in the request echo so saved diagnostics identify their exact scope.
Operation intents use a 300-second HTTP budget for the existing bounded host
probes; an explicit global `--timeout` still wins. Ordinary reads keep their
existing budget. This is still one read and grants no apply authority. Launchplane#3911 owns compatible service delivery.

The service's `check` becomes `result`: product, path, overall state, blocked
and unknown counts, and ordered steps with `step_id`, state, code, fixed
description, fix kind, and record ids. A failed testing deploy's code is the
operation's error code: up to four dot-separated snake_case words (for example
`plan_not_ready.<blocker>`), at most 96 characters. Colons and other shapes
fail closed. Clear steps use `fix: none`; other fix
kinds are `code`, `grant`, `owner_approval`, `client_acceptance`, `by_hand` and
`wait`. Counts and overall state must agree with the steps, and the response
must match the requested product and path. Unknown fields at every level,
unsafe names or values, missing fields and invalid enums fail closed. Empty
record-id lists are valid when the service has no record id for a step. Lists
are limited to 50 steps and 50 record ids per step; oversized responses fail
instead of omitting blockers. A successful read can still describe a blocked
or unknown path; inspect `result.state` and every step before proposing action.
Operation record ids may include words such as `task` or `helpdesk` followed
by a hex suffix; an embedded `sk-` at the end of a word is not a token prefix.
Actual token prefixes at a boundary remain rejected. Free-text descriptions
still reject tokens embedded in words or escaped log text. On a projection failure,
the result stays empty and the response's independently validated trace id is
retained in `summary.trace_id`; an unsafe or malformed trace is omitted.

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

Strict projections check each response object against a fixed field
allowlist, so one unknown field there fails the whole read with
`unsafe_response_shape`; some reads, such as activity and target-replacement
operations, drop or list unknown fields instead. A Launchplane PR that adds a
field to a strictly projected response lands the matching helper allowlist
change first and says so in its PR body.

For bounded GET reads, the top-level allowlists live in
`BOUNDED_READ_RESPONSE_FIELDS` in `scripts/launchplane_contract.py`. A field
Launchplane declares there but the read does not project, such as the
environment read's `host_network_capacity_available` and
`host_network_capacity`, is accepted and dropped; any other field still fails
closed. `references/launchplane-read-response-fields.json` pins the top-level
fields Launchplane's generated OpenAPI declares for each of those routes, and
the tests require every pinned field to be in its read's allowlist. Refresh the
pin by running `uv run launchplane service export-openapi --output <schema-path>`
from a Launchplane source checkout, then
`uv run scripts/sync-read-response-fields.py sync --openapi <schema-path>` from
this skill directory. Add each new field to its read's allowlist, projecting it only
when the read should emit it, and name the Launchplane commit in the commit
message. The `Launchplane Read Response Fields` workflow fails on pull
requests, on `main` and daily when the pin differs from a fresh source export
of Launchplane's `main`. It does not fetch the retired committed snapshots. The
comparator exits 0 for agreement, 1 for drift and 2 when it cannot derive the
fields from the supplied schema.

The merge-train controller reply has no helper allowlist. Launchplane publishes
its contract as `contracts/merge-train-controller-reply.schema.json` and tests
every reply against it; this skill pins a byte-for-byte copy at
`references/merge-train-controller-reply.schema.json`. The helper enforces the
pin's types, required fields and bounds. A field the pin does not declare is
newer than the pin: it is left out of the result, named in
`summary.unrecognized_fields` (plain identifiers only, at most ten) with a
`controller_reply_newer_than_pinned_schema` warning, and does not fail the
read. A field name that is not a plain public-safe identifier, or any unsafe
value, still fails closed. Refresh the pin with
`uv run scripts/sync-merge-train-reply-schema.py sync` from this skill
directory, naming the Launchplane commit in the commit message; the
`Merge-Train Reply Schema` workflow fails on pull requests, on `main` and daily
when the pin differs from Launchplane's `main`.

Unexpected successful provider shapes fail closed as `invalid_response` rather
than being recursively sanitized. For an accepted controller reply that cannot
be projected, `summary.projection_error_code` and the warning code retain the
local `LaunchplaneSafetyError` code (such as `unsafe_response_shape` or
`invalid_response`). No rejected value is emitted. Independently validated service error
codes and trace IDs remain available; read the PR and controller state before
retrying a mutating pass, which may already have completed.
Summary fields, including `trace_id`, must be
compact safe identifiers or bounded safe text. Keys or nested payloads that look
like secrets, credentials, cookies, tokens, plaintext values, private keys,
opaque values, raw requests, provider env, or headers are not copied into output.

Unauthorized, unavailable, denied, stale, and mismatched-intent responses keep
the same envelope and include compact `summary.error_code`, `summary.trace_id`,
and `warnings` entries. They do not include raw request bodies.

Preview-feedback projection failures retain independently validated
`summary.error_code`, `summary.trace_id` and transport `summary.http_status`
when available, plus the local `summary.projection_error_code`. An unsafe
diagnostic is dropped without discarding its safe neighbors. Unparseable or
interrupted bodies retain the received HTTP status; missing service codes or
traces are never reconstructed from body text, exception messages or headers.

The private saved Client-review reader uses the same diagnostic projection:
failed reads remain nonzero with `ok=false` and the existing generic `error`,
adding bounded `error_code`, `trace_id` and `status_code` when available. It
does not emit provider prose, URLs, credentials or arbitrary error fields.

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
For an integration-allowance plan's reason only, a summary-denied marker left
after sanitization redacts the whole field: it may be an unrecognized assignment
with an adjacent value. Thus "credentials are removed" does not discard an
otherwise valid plan. Its review remains bound by the service's plan digest.
Other prose retains strict summary validation, including callers that compare
reviewed reasons.
Structural fields retain their existing strict validators; invalid types,
empty or oversized text, and other unsafe summary shapes still fail closed.
Apply checks that compare a reviewed reason use this projection on both sides.
The private request keeps the original reason; redaction changes public evidence
only. A projection is lossy and cannot distinguish changes solely inside
redacted spans; service digests and private-payload bindings remain in force
where supported.

## Managed runtime sync

`live-target-runtime-sync-dry-run` and `live-target-runtime-sync-apply` use the
existing `POST /v1/live-target-runtime/apply` helper binding. Pass `--product`,
`--context`, and `--instance`; values come from Launchplane's managed records,
never CLI input. The service checks `live_target_runtime.plan` for dry runs and
`.apply` for writes on product/context. The helper's apply command requires both
permissions because it also runs pre-apply and read-back dry runs. It grants
nothing and never switches identity.

Repeat `--key NAME` to select exactly those existing managed keys. Use the
same selection on dry-run, apply and readback. Duplicate and malformed names
are rejected before any request. Selected sync applies
no retirement and preserves unselected provider values and stored retirement
intent. Its readback verifies only the selected keys, so unrelated drift can
remain. Omitting `--key` keeps the full-lane sync behavior. The helper requires
the service to echo the selection and rejects changes outside it, including
retirements. The selection is part of the reviewed plan digest.

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

For event-driven generic-web recovery, use the service-owned reference read
described under [Generic-Web Deploy Recovery](#generic-web-deploy-recovery).
`deploy_key_sha256` identifies a key but cannot reconstruct it. If the service
cannot supply exact evidence, leave recovery held.

## Workflow-rule retirement inspection

`workflow-rule-retirement-inspect --payload-file <private-file>` and
`workflow-rule-retirement-plan --payload-file <private-file>` are bounded local
extensions of `POST /v1/authz-policies/workflow-rule-retirement-plan`. They are
reads. The service writes nothing, and the helper sends no idempotency key.
There is no apply mode. Retiring a rule needs a separate reviewed policy change.

The private JSON file holds exactly `policy_record_id`, `policy_revision`,
`policy_sha256` and `selection`. Take them from the active policy. `selection`
lists 1 to 50 unique rules. Each has `rule_sha256` and both `managed_set_id`
and `managed_rule_id`: both `null` for an unmanaged rule, both set for a
managed one. Do not put `mode` in the file; the subcommand sets it. Keep the
file outside the repository. The helper refuses a bad file before any call.
The echoed request shows only the mode, `payload_source` and the selection
count, never the path or a digest.

Each selected rule returns its fingerprint, management, managed ids,
`broad_or_shared_scope` and `scope`. Scope keeps the repository, its numeric
ids, and the workflow ref, job workflow ref, event, ref, environment, product,
context, instance and action selectors. Selectors may hold glob characters.
A value the helper cannot verify is dropped, its path is listed in
`dropped_field_paths`, and that rule's `scope_complete` is `false`. Unlisted
fields are dropped the same way. A token-like value anywhere in scope makes
the whole read `invalid`. The result also keeps `blockers` (for example
`last_administrator_protection`) and `apply_supported`, which is always
`false`. The plan adds `candidate_policy_sha256`,
`surviving_workflow_rule_count`, `survivor_rules_sha256`,
`other_principal_collections_preserved` and `plan_sha256`.

`dependency_verification: unknown_requires_source_review` means Launchplane
does not know which workflows use these rules. Read the workflow sources
before you propose a retirement.

The helper checks the response against the request. A different policy
record, revision or digest, a missing or extra rule, a wrong management value,
`apply_supported` other than `false`, plan fields on the wrong mode, a missing
or mistyped repository or selector field in a scope (only null managed ids may
be absent), or any `dependency_verification` other than
`unknown_requires_source_review` return `invalid`. A 409
`workflow_rule_selection_changed` or `active_authz_policy_ambiguous` returns
`stale`: re-read the active policy and the rules, then rerun. A 400 returns
`invalid_request`, and a 403 returns `denied` with its trace. A client timeout
returns `unavailable` with the error code `client_timeout`. The service
needs service-wide policy read authority to show full scopes.

## Runtime-setting removal review

`runtime-setting-removal-dry-run` and `runtime-setting-removal-readback` are
bounded helper bindings of `POST /v1/admin/runtime-settings/removal`. They
let the Director agent review removing stored plain runtime settings and read
back the result. There is no apply command. Apply belongs to the signed-in
admin in Launchplane, and a dry-run digest does not authorize it.

Inputs are flags, not a file: `--product`, `--scope` (`global`, `context` or
`instance`), `--context`, `--instance`, one `--key` per stored setting name,
and `--reason`. Readback also needs `--review-id` and `--plan-sha256`. Global
scope needs product `launchplane` and no context or instance. Context scope
needs a context and no instance. Instance scope needs both. Keys must be
unique printable setting names, not token-shaped, at most 256. The reason is
required, at most 1,000 characters, and sent to the service but never printed.
The helper refuses a bad request before any call. Neither mode sends an
idempotency key.

The echoed request shows the mode, product, scope, context, instance, keys,
`payload_source: operator_argument`, and on readback the review id. It never
shows the reason or the digest.

A dry run saves a private service-owned review, including the original record
for rollback, and returns a fresh `review_id` and `plan_sha256` on every call.
The result keeps the scope fields, `removed_keys`, `surviving_keys` (capped at
500, with `surviving_keys_truncated`), `unprintable_survivor_count`,
`surviving_value_count`, `retirement_intent_count`, `record_deleted`,
`requires_compatible_readers_and_rollback`, `record_sha256`,
`authority_sha256`, `plan_sha256`, `review_id`, `updated_at`, `applied`
(always `false`) and `apply_supported` (always `false`). Readback adds
`readback_matches` and `current_record_present`. An odd surviving name or a
malformed `updated_at` is dropped and listed in `dropped_field_paths`, as are
unlisted fields.

The helper checks the response against the request. A different product,
scope, context or instance, removed keys that differ from the requested keys,
`applied` other than `false`, any apply-only field (`applied_at`,
`audit_review_id`, `replayed`), a malformed digest, a `review_id` other than
`runtime-removal-` plus `plan_sha256`, a mistyped flag or count, a
`surviving_value_count` below the number of surviving names, readback fields
on a dry run or missing ones on readback, or a token-shaped surviving name
returns `invalid`. On readback the review id and digest must also equal the
ones sent.

Readback must reuse the dry run's exact keys, reason, review id and digest;
the service compares the saved review's intent with the request.
`readback_matches: false` means drift or the removal is not applied yet. It
is not success.

`requires_compatible_readers_and_rollback: true` means the removal takes the
last stored value from a record that keeps provider-retirement intent, so the
record survives with no values. Before any admin apply, deploy readers that
accept such records everywhere and keep a rollback build with that support.
The flag reports the requirement; it does not attest deployment or approve it.

A 409 `runtime_setting_removal_unverified` returns `stale`: refresh the
names-only runtime-settings read and run a fresh dry run, or for readback
reuse the dry run's exact keys, reason, review id and digest. A 403
`authorization_denied` returns `denied`, a 401 `unauthorized`, a 503
`database_storage_required` `unavailable`, and a 400 `invalid_request`, each
with its trace. A client timeout is `unavailable` with the error code
`client_timeout`.

## Policy Proposals

`privileged-policy-propose --payload-file <private-envelope>` is a bounded local
extension of `POST /v1/agent/privileged-operations/plans`. Use the normal private
`local_operator` configuration. It accepts `managed-authz-policy-set`, `managed-merge-train-policy-import`, and
`production-backup-policy`. The service is the request-schema authority;
see [Launchplane's proposal contract](https://github.com/cbusillo/launchplane/blob/main/docs/privileged-operations.md#agent-policy-proposals).

The private JSON envelope contains `schema_version: 1`, `descriptor_id`, a
stable `source_event_id`, `request` (including reason and optional related issue),
and optional `expires_in_seconds`. Never put it in a repository or pass policy
JSON inline. The helper rejects dedicated preparation contexts and unsupported
request fields. It emits bounded proposal metadata, counts, trace,
and a full `review_url` built from the configured service URL. Use that clickable
URL in Director questions and handoffs; the relative `review_path` remains for
compatibility. This points to the UI on that same service base, including any
configured path prefix; a tunnel or API-only proxy is not proof that the
Director's browser can reach it. Verify the browser-facing address before the
handoff if the configured base does not expose the signed-in UI.
The URL identifies the private service host, so share it only
with destinations allowed to know it. The helper never returns policy selectors
or credential-routing fields.

For selected unmanaged workflow-rule removal, `managed-authz-policy-set` also
accepts `request.workflow_rule_retirement`, containing the producer's exact
active policy snapshot and fingerprint selection with `mode: plan`. Supply an
empty `managed_set_id` and an empty schema-v2 or schema-v3 `desired_policy`.
The producer rejects mixed policy changes and managed selections; managed rules
are contracted through their own reviewed set. This field stays in the private
envelope and is never echoed. The existing proposal metadata, full review URL,
signed-in Director approval and replay rules are unchanged. See the producer's
[reviewed retirement contract](https://github.com/cbusillo/launchplane/blob/main/docs/reviewed-retirement.md#reviewed-workflow-rule-removal).

For `production-backup-policy`, `request` contains `authority` (the existing private
backup-authority envelope with `mode: dry_run` and no reviewed digest), `reason`,
and optional `related_issue`. The service runs and pins the dry run, saves the
proposal inertly and returns a stable product production Actions link. The helper
binds that link to the submitted product and proposal ID; provider coordinates,
free-text reasons and the private authority envelope never reach its output.
Backup proposals use existing exact-lane backup planning authority. The signed-in
Director approves or declines in the card, from any tab or device, with no file
upload or digest comparison. The proposer cannot decide or directly apply a
backup policy, even with a backup-write grant. The older direct-apply helper
command does not override this: the service refuses `local_operator`/`terminal_agent`
application, and approval stays in the signed-in Director card. Before use, verify that the service
supports the descriptor; source delivery does not activate it or grant access.

`merge-train-trusted-automation-propose --change add|remove --github-user-id ID
--repo OWNER/REPO ... --reason TEXT --source-event-id ID` builds the
`managed-merge-train-policy-import` envelope itself. It reads the active policy
through the `merge-train-policy-read` route, changes only
`enqueue.trusted_automation_github_user_ids` on the named repositories' targets,
and submits the result through the same proposal route and output. The record,
including credential-routing fields, stays in memory and is never written to
disk. The candidate's `policy_sha256` is left for the service to compute, and
its `record_id` is `merge-train-policy-<updated_at>-proposal`. The request in the
output names the repositories, the active record id and digest, the changed
target count and `updated_at`. To replay an uncertain submission, rerun with the
same source event, `--updated-at` and `--expected-active-record-id` set to the
reported `active_record_id`; that rebuilds the identical envelope. If the replay
stops with `active_policy_changed` or `policy_unchanged`, the policy moved or the
plan already ran: read the plan's persisted review instead of resubmitting. An
unenrolled repository (`repository_not_enrolled`) or a change that alters nothing
(`policy_unchanged`) stops before any write.

`merge-train-enrollment-propose --repo OWNER/REPO --repository-id ID --like
OWNER/ENROLLED [--base-branch main] --reason TEXT --source-event-id ID` uses the
same read, envelope, output and replay rules. It appends a copy of the `--like`
repository's target for that base branch with the new `repository` and
`github_token.github_app.repository_id`, so the new target inherits that
target's labels, enqueue authority, merge identity, App credential binding and
scheduler settings for the Director to review. It leaves out the template's
own `provider_delivery_protection_expectation` and
`config_authority_events_enabled`, which describe the template repository. It
stops before any write when the repository branch is already enrolled
(`repository_already_enrolled`), the template branch is not
(`template_not_enrolled`), or the template uses any credential source besides
a GitHub App (`template_not_github_app`). The service's review still decides
whether the App installation and required checks support it.

On a timeout, HTTP 5xx or response-verification failure, retain the same envelope and
source event for reconciliation/replay. Never substitute a new identity or a new
source event to force a duplicate proposal. A denial is not a missing token:
report the refused action and trace and follow Launchplane denial handling.
The helper preserves an empty `expires_at` and normalizes valid legacy timestamps
for display. Review validity belongs to
[Launchplane's approval and execution contract](https://github.com/cbusillo/launchplane/blob/main/docs/privileged-operations.md#approval-and-execution-boundary).
An accepted replay can report an already approved or terminal plan; it does not
perform execution. Check the persisted review and blockers in the UI before
requesting approval. Approved or executing plans need observation, not another
approval. Inspect terminal outcomes before an intentional replacement; an expired, cancelled or revoked proposal needs a fresh
source event for a replacement. The signed-in Director reviews
and approves pending plans in the UI; no helper approval or apply path is added.

## Pending Client review withdrawal

`product-review-withdrawal-dry-run`, `product-review-withdrawal-apply` and
`product-review-withdrawal-readback` use `POST /v1/product-review/withdrawal`.
They require `--product`, `--repo OWNER/REPO`, `--pr`, `--head` (the exact current
commit), `--reason` and `--director-decision` (the recorded decision's GitHub
comment URL). The Director's authenticated agent or admin attests that this
citation records the Director's decision; it supplies no Client acceptance.
No new grant or credential is installed.

Save the dry-run JSON output outside the checkout. Apply requires
`--reviewed-dry-run --dry-run-evidence-file /private/dry.json`,
`--expected-plan-digest` from that output and `--idempotency-key`. Saved evidence
binds the exact scope, head, reason and decision citation; receipts omit the
reason and citation. Readback requires that same scope, reason, citation,
expected digest and original key. Apply always follows with readback, and only
matching applied readback returns success. Drift, malformed responses and
transport loss exit nonzero; an uncertain apply reports `outcome_unknown`, and
an applied response without verified readback reports `accepted_unverified`.

The service refuses after a Client has accepted or rejected this product's
current head. It removes only the configured review label and records the
withdrawal in the PR's history and its durable operation. It never edits Client
decisions, PR test notes, or release acceptance. Truthful engineering notes are
a separate PR edit; real notes or a later review label request review normally.

Keep the original request, reviewed digest and key after an interrupted call.
Read back before resubmitting the exact apply. Recovery can finish a missing
history entry when the label is absent, and never deletes a label again after
an uncertain dispatch. A pre-dispatch failure can resume the original operation.
If a request is present after dispatch, or its head/review authority changed,
recovery instead settles the original intent with `applied=false` and
`replan_required=true`, without claiming absence of earlier effects. The helper
reports `replan_required` and exits nonzero. Review a fresh dry run and use a new
key to withdraw that request. `product_review_withdrawal_unsettled` is an
`outcome_unknown`, distinct from a definite refusal.
A completed-key replay returns the original receipt, preserving later review
requests. Do not remove a label by hand or use another identity after a denial.

The helper classifies the whole exchange, keeping status, retained receipt and
recovery instructions together. Attempted-apply transport loss or any HTTP 5xx
is `outcome_unknown`. A known applied receipt without matching readback is
`accepted_unverified`, and that receipt stays in the output. An unfinished
original readback is `outcome_unknown` (or `in_progress` while running).
`target_busy` means another operation owns the PR and this call dispatched
nothing: only that operation's original caller/key can recover it. A key conflict
is `conflict`; a definite eligibility refusal is `refused`. Neither is reported
as an unknown effect. Terminal `replan_required` results preserve the boolean
`prior_effects_unknown`; when true, a label removal may have happened without a
PR history entry. Inspect the retained operation and history before a fresh plan.

Recovery can observe a closed PR. It adopts an authentic existing history
receipt or settles an unreceipted closed request without another DELETE.
A new withdrawal still requires an open PR; readback never removes a label.

Recovery output echoes the non-secret `idempotency_key` in its summary. Choose a
public-safe opaque key, and save the exact private request with the dry-run
evidence. Provider evidence failures are unavailable, not Client eligibility
refusals. Dry-run and fresh apply check history before removing a label. Bounded
or ambiguous authenticated history blocks dispatch with `history_unverifiable`;
resolve history verification before retrying, because changing keys cannot fix
it. Recovery of an uncertain operation can settle with that same status while
retaining `prior_effects_unknown`. That key is terminal: after resolving history
verification, review a fresh dry-run and use a new key. Completed readback preserves its applied
receipt and reports a mismatch when permanent history limits prevent verification.
`not_started` means this caller/key has no saved operation: resubmit the exact
original apply with that same key. `invalid_request` requires correcting the
request; `stale_plan` requires refreshing and reviewing the exact dry-run. The full caller identity remains in the durable
operation; public PR history contains the head, reason and Director citation.
