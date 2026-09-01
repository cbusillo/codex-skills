# Launchplane Write-Action Helper Contract

This contract defines the public-safe wrapper for bounded Launchplane operator
actions. It is separate from `launchplane-context.py`: read-only context remains
optional and soft-failing, explicit write actions fail closed, and bounded
operator reads also fail closed when required configuration or authorization is
missing.

Projected operation paths are resolved from the vendored
`agent-operator-contract.json` through `launchplane_contract.py`. Run
`check-agent-operator-contract.py` to verify schema, semantic digest,
public-safety, operation semantics, protected workflows, invariant coverage, and
helper bindings without network access. This is local consistency evidence, not
proof of upstream freshness.

Generic-web deploy recovery is intentionally listed as a bounded local
extension because its two routes are not in the current upstream projection.
The conformance gate fails if those routes later appear upstream so migration
cannot leave duplicate route authorities behind.

The helper lives at `scripts/launchplane-write-action.py`.

For stale Launchplane-managed preview comments that cannot be replayed under a
current workflow identity, use `preview-feedback-remediation`. Run with
`--mode dry-run` first, then repeat the exact target, terminal status, reason,
related issue, and idempotency key with `--mode apply --reviewed-dry-run`.
The helper derives the service-required confirmation phrase and projects only
bounded observation and mutation evidence; it never accepts a raw comment id or
arbitrary marker.

## Configuration

The helper uses this private operator config source order:

1. `--config /path/to/local-operator.json`
2. environment variables in the current process
3. `~/.config/launchplane/local-operator.env`
4. `~/.config/launchplane/local-operator.json`

The committed example is fake and public-safe:

```json
{
  "service_url": "https://launchplane.example.invalid",
  "admin_token_env": "LAUNCHPLANE_LOCAL_ADMIN_TOKEN",
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
`LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL`, plus the distinct
`LAUNCHPLANE_LOCAL_ADMIN_TOKEN` used by the activation-preflight read. The
local-admin read never falls back to the operator token. The helper may also notice
`LAUNCHPLANE_PUBLIC_URL` as a diagnostic near-miss when the operator URL is
missing, but it does not use that variable as write authority.

For public-safe diagnostics, use:

```sh
uv run launchplane/scripts/launchplane-write-action.py operator-config-diagnostic
uv run launchplane/scripts/launchplane-write-action.py --url <operator-url> operator-config-diagnostic
```

The diagnostic reports source presence, token presence, and which source won. It
does not print token values, subjects, labels, URLs, headers, or request bodies.
Global options such as `--url` must appear before the subcommand. A diagnostic
with `status: "incomplete"` may still have a local token source; read the
`classification` field before describing the failure.

## Exit Behavior

- `0`: Launchplane accepted the request and the helper emitted a redacted
  summary. For `status: "accepted_unverified"`, the write may have committed;
  read back the active record before any retry.
- `1`: Launchplane was reached but rejected the request, or the service was
  unavailable/invalid.
- `2`: The requested write action could not be attempted because local operator
  config was missing/invalid or the helper request was malformed.

Missing Launchplane config is still non-fatal for skills that only need context;
it is a fail-closed result for this helper because every command is an explicit
operator operation.

## Authorization Activation Preflight

The supported service-native preflight resolves an existing GitHub-human
session server-side and evaluates its exact `authz_policy_grant.write` access:

```sh
uv run launchplane/scripts/launchplane-write-action.py \
  authz-activation-preflight-read \
  --github-id <bounded-positive-github-id>
```

The helper reads the service URL from the normal operator URL sources and the
bearer credential only from `LAUNCHPLANE_LOCAL_ADMIN_TOKEN` or an explicitly
configured `admin_token_env`. It accepts no token argument and sends exactly
`{"github_id": <signed-64-bit positive integer>}` to
`POST /v1/authz-diagnostics/activation-preflight/read`. It projects only the
trace, active policy identity, coarse session freshness, keyed identity
fingerprint, fixed evaluated scope, decision/reason, and bounded unmanaged
action-empty counts. Unexpected or additional fields fail closed. This read
does not authorize or perform an activation, policy grant, session renewal,
reconciliation, secret mutation, or direct database access.

Configuration and error states are distinct:

- `ambiguous_service_url`: token exists and `LAUNCHPLANE_PUBLIC_URL` is present,
  but no operator URL source is configured. Obtain the correct operator URL and
  pass it with `--url` before the subcommand, or configure
  `LAUNCHPLANE_OPERATOR_URL`.
- `missing_service_url`: token exists but no write-capable service URL source is
  configured. Fix local operator routing by configuring
  `LAUNCHPLANE_OPERATOR_URL`, passing `--url` before the subcommand, or
  supplying a private JSON `service_url`. This is local routing, not evidence
  that an administrator credential should exist.
- `missing_local_admin_token`: service URL exists but no already-sanctioned
  local-admin credential source is configured. Treat this as an architecture
  gap, not a provisioning instruction.
- `missing_local_admin_config`: neither the service URL nor an already-sanctioned
  local-admin credential source is configured. Configure only a documented
  operator URL; do not create or substitute a credential for this compatibility
  helper.
- `unauthorized`: Launchplane rejected the credential, usually HTTP 401.
- `denied`: Launchplane accepted the credential but denied the specific action,
  usually HTTP 403 or `authorization_denied`. This is an authority-scope result,
  not a credential-selection result. Report it with the trace ID, block only the
  affected work, and continue independent safe work when available. Escalate a
  capability gap to the owning authorization-architecture issue. Do not probe
  routes manually or route the call through a GitHub workflow, Actions secret,
  or OIDC role.
- `stale`: retry requires refreshed dry-run or intent evidence.
- `unavailable`: service/network/response failure; do not switch to provider
  mutation.

`operator-config-diagnostic` reports ordinary local-operator readiness only. It
may report `ready` while local-admin custody is absent, so it is not readiness
evidence for this compatibility preflight.

## Product Config

For helper-driven product-config work, agents should start with intent
preflight. This validates authorization and managed-secret binding policy
without accepting plaintext values:

```sh
uv run launchplane/scripts/launchplane-write-action.py \
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

When a trusted local owner already has an explicit private payload file outside
the repo, the helper can submit the documented product-config route:

```sh
uv run launchplane/scripts/launchplane-write-action.py \
  product-config-dry-run \
  --payload-file /private/path/product-config-request.json \
  --idempotency-key example-product-config-dry-run-123

uv run launchplane/scripts/launchplane-write-action.py \
  product-config-apply \
  --payload-file /private/path/product-config-request.json \
  --reviewed-dry-run \
  --idempotency-key example-product-config-apply-123
```

The payload file is explicit private operator input. Do not commit it, paste it,
log it, or summarize its raw contents. It must live outside the active
repository or worktree so checked-in config and examples cannot quietly become
write payloads. Local-operator apply still requires a prior matching dry-run
recorded by Launchplane.

Unsupported secret source shapes must fail closed in caller guidance. Do not
translate committed secret references, provider env lookups, stdin/stdout
transport, arbitrary secret ids, or "reuse current value" requests into a
product-config request.

Unsupported runtime-authority shapes must also fail closed. Do not translate
checked-in product maps, workflow defaults, copied provider route payloads,
repository bindings, branch bindings, tenant/domain lists, lanes, provider target
ids, authz grants, or operator identities into product-config requests unless
they came from Launchplane records or explicit scoped operator input.

If a denied or unsupported operation concerns authz grants, private health
endpoint records, provider targets, route records, or operator/workflow grants,
do not widen the local helper and do not substitute GitHub CI authority. An
already-sanctioned, Launchplane-owned reconciliation entrypoint may be run
unmodified when the operator initiates it for that record. Otherwise this is a
capability gap: block and escalate the affected work to the owning
authorization-architecture issue with the denied operation, record type, and
trace ID, then continue independent work when possible.

`missing_local_admin_token` and `missing_local_admin_config` on the activation
preflight are architecture-gap results unless an already-sanctioned private
credential source is documented. Do not provision, discover, extract, or
substitute a credential merely to satisfy this compatibility helper.

## Change-Impact Policy

Change-impact policy is runtime authority. Supply it only as explicit private
operator input in a JSON file outside the active repository or worktree. The
file contains the service envelope, including `record`, concurrency expectations,
and the operator-owned source and reason. The helper overrides only `mode`.

```sh
uv run launchplane/scripts/launchplane-write-action.py \
  change-impact-policy-dry-run \
  --payload-file /private/path/change-impact-policy.json

uv run launchplane/scripts/launchplane-write-action.py \
  change-impact-policy-apply \
  --payload-file /private/path/change-impact-policy.json \
  --reviewed-dry-run \
  --expected-policy-digest <digest-from-dry-run> \
  --idempotency-key example-repository-policy-revision-1

uv run launchplane/scripts/launchplane-write-action.py \
  change-impact-policy-read \
  --repository-id <repository-id>
```

For apply, the operator asserts that the private payload is the one reviewed
during dry-run. The helper requires a non-empty reason embedded in the policy
record, explicit `--reviewed-dry-run` acknowledgement, the policy digest emitted
by dry-run, and a stable idempotency key. The helper refuses to send apply when
the supplied digest differs from a digest already present in the payload. When
the record omits its server-derived digest, the helper inserts the reviewed
dry-run digest before apply. These are local operator controls; the current
service route does not independently bind apply to a
persisted dry-run record or consume the idempotency header. After apply, run the
bounded read command and compare the active revision and digest before relying
on the policy.

The public result contains only the apply status, policy record id, digest,
revision, policy status, effective timestamp, and trace metadata. It never emits
component rules, path prefixes, affected products, repository name, owner ID,
source, reason, raw payloads, private paths, service URLs, or authorization
headers. The record id intentionally embeds the numeric repository ID because
successor policy revisions must reference it. Unexpected response fields fail
closed. If apply receives HTTP success but the response cannot be safely
projected, the helper reports `accepted_unverified` and requires read-back before
any retry.

## Generic-Web Deploy Recovery

Generic-web deploy recovery is a bounded admin operation for recovering a
generic-web product instance from a failed deploy. Supply the private payload
only as explicit operator input in a JSON file outside the active repository or
worktree. The file contains `schema_version`, `product`, `instance`,
`original_deploy`, and `reason`. The apply request additionally requires
`expected_recovery_digest`, which the helper inserts from the `--expected-recovery-digest`
flag after verifying it against any value already in the payload and the saved
redacted dry-run evidence.

Both dry-run and apply require `--idempotency-key`. The idempotency key must be
the original deploy's idempotency key for both calls — it is sent as the request
`Idempotency-Key` header exactly as supplied.

```sh
uv run launchplane/scripts/launchplane-write-action.py \
  generic-web-deploy-recovery-dry-run \
  --payload-file /private/path/deploy-recovery.json \
  --idempotency-key <original-deploy-idempotency-key>

uv run launchplane/scripts/launchplane-write-action.py \
  generic-web-deploy-recovery-apply \
  --payload-file /private/path/deploy-recovery.json \
  --idempotency-key <original-deploy-idempotency-key> \
  --reviewed-dry-run \
  --expected-recovery-digest <recovery-digest-from-dry-run> \
  --dry-run-evidence-file /private/path/recovery-dry-run-output.json
```

For apply, the operator asserts that the private payload is the one reviewed
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
uv run launchplane/scripts/launchplane-write-action.py \
  merge-train-controller-run-once \
  --repo example/repo \
  --base-branch main

uv run launchplane/scripts/launchplane-write-action.py \
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
  codes, commit ids, source/workflow URLs, and merge-train record ids.
- `product-config-preflight`, `product-config-dry-run`, and
  `product-config-apply` may emit only intent status, reason code,
  safe-to-execute, next action, managed binding keys, runtime key-safety finding
  codes, and product-config/intent record ids.
- `change-impact-policy-dry-run` and `change-impact-policy-apply` may emit only
  apply status, policy record id, digest, revision, policy status, and effective
  timestamp.
- `change-impact-policy-read` may emit only shadow/enforcement status, history
  count, and the same bounded current-policy metadata.
- `authz-activation-preflight-read` may emit only active policy identity,
  coarse session freshness, keyed identity fingerprint, the fixed
  `authz_policy_grant.write` scope, evaluation decision/reason, and bounded
  unmanaged action-empty counts.

The projections recognize the current service envelopes, including idempotent
replay metadata, nested merge-train candidate/landing/stack summaries, the
write-intent `record`, and product-config runtime, key-safety, count, and secret
binding metadata. Secret record ids, provider target details, actor fields,
instructions, raw findings, and arbitrary nested dictionaries are not copied.

Unexpected successful provider shapes fail closed as `invalid_response` rather
than being recursively sanitized. Summary fields, including `trace_id`, must be
compact safe identifiers or bounded safe text. Keys or nested payloads that look
like secrets, credentials, cookies, tokens, plaintext values, private keys,
opaque values, raw requests, provider env, or headers are not copied into output.

Unauthorized, unavailable, denied, stale, and mismatched-intent responses keep
the same envelope and include compact `summary.error_code`, `summary.trace_id`,
and `warnings` entries. They do not include raw request bodies.
