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

Merge-train policy import, repository inventory, and generic-web deploy
recovery are intentionally listed as bounded local extensions because their
routes are not in the current upstream projection. The conformance gate fails
if those routes later appear upstream so migration cannot leave duplicate route
authorities behind.

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
  unavailable/invalid. For `status: "outcome_unknown"`, transport failed after
  an apply POST began; read back the active record before any retry.
- `2`: The requested write action could not be attempted because local operator
  config was missing/invalid or the helper request was malformed.

Missing Launchplane config is still non-fatal for skills that only need context;
it is a fail-closed result for this helper because every command is an explicit
operator operation.

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

## Merge-Train Policy Import

Merge-train policy records are runtime authority. Supply the exact service
envelope only through a private JSON file outside the active repository or
worktree. The top-level fields must be exactly `schema_version`, `product`,
`mode`, `reason`, and `record`; the helper does not rewrite the requested mode.

```sh
uv run launchplane/scripts/launchplane-write-action.py \
  merge-train-policy-import-dry-run \
  --payload-file /private/path/merge-train-policy-dry-run.json \
  --expected-current-policy-digest <active-policy-digest>

uv run launchplane/scripts/launchplane-write-action.py \
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
the active policy can still change between the read and the POST. Operators must
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
uv run launchplane/scripts/launchplane-write-action.py \
  repository-inventory-read \
  --repository-id <repository-id>

uv run launchplane/scripts/launchplane-write-action.py \
  repository-inventory-dry-run \
  --payload-file /private/path/repository-inventory.json \
  --idempotency-key <stable-key> \
  > /private/path/repository-inventory-dry-run.json

uv run launchplane/scripts/launchplane-write-action.py \
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

Public-safe output omits repository name, owner ID, source, reason, raw payload,
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

When the controller returns `controller_action: block`, the helper preserves a
public-safe `blocking_reason` code and message plus the bounded merge-readiness
facets (`state`, reason codes, owner states, technical checks, engineering
review, policy, candidate, and fence). Unexpected nested fields or secret-like
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
