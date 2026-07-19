# Launchplane Write-Action Helper Contract

This contract defines the public-safe wrapper for bounded Launchplane write
actions. It is separate from `launchplane-context.py`: read-only context remains
optional and soft-failing, while explicit write actions fail closed when
operator configuration or authorization is missing.

The helper lives at `scripts/launchplane-write-action.py`.

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
  summary.
- `1`: Launchplane was reached but rejected the request, or the service was
  unavailable/invalid.
- `2`: The requested write action could not be attempted because local operator
  config was missing/invalid or the helper request was malformed.

Missing Launchplane config is still non-fatal for skills that only need context;
it is a fail-closed result for this helper because every command is an explicit
write-capable operation.

Configuration and error states are distinct:

- `ready`: operator URL and token sources are present. This is not proof that a
  token is authorized for every action.
- `ambiguous_service_url`: token exists and `LAUNCHPLANE_PUBLIC_URL` is present,
  but no operator URL source is configured. Obtain the correct operator URL and
  pass it with `--url` before the subcommand, or configure
  `LAUNCHPLANE_OPERATOR_URL`.
- `missing_service_url`: token exists but no write-capable service URL source is
  configured. Fix local operator routing by configuring
  `LAUNCHPLANE_OPERATOR_URL`, passing `--url` before the subcommand, or supplying
  a private JSON `service_url`, then rerun `operator-config-diagnostic`. This is
  local operator setup, not PR readiness, merge-train admission, or scheduler
  state.
- `missing_operator_token`: service URL exists but the local operator token is
  missing.
- `missing_operator_config`: both service URL and token are absent.
- `unauthorized`: Launchplane rejected the credential, usually HTTP 401.
- `denied`: Launchplane accepted the credential but denied the specific action,
  usually HTTP 403 or `authorization_denied`. For higher-authority runtime
  records, check the Launchplane authz reconciliation or GitHub Actions OIDC
  path before manual route probes.
- `stale`: retry requires refreshed dry-run or intent evidence.
- `unavailable`: service/network/response failure; do not switch to provider
  mutation.

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
look for the Launchplane-owned reconciliation surface instead of widening the
local helper. In Launchplane repositories this may be a deploy workflow or
authz-grant reconciliation script run through GitHub Actions OIDC.

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
