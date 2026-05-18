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

When `--config` is supplied, its `service_url` is the explicit write target
unless `--url` is also supplied. The helper does not also load the default `.env`
file in that case, but it does honor an explicit `--env-config` for token,
subject, and label values. When no explicit JSON config is supplied, the helper
may load `~/.config/launchplane/local-operator.env` for these keys only:
`LAUNCHPLANE_OPERATOR_URL`, `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN`,
`LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT`, and
`LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL`.

For public-safe diagnostics, use:

```sh
uv run launchplane/scripts/launchplane-write-action.py operator-config-diagnostic
```

The diagnostic reports source presence, token presence, and which source won. It
does not print token values, subjects, labels, URLs, headers, or request bodies.

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

The payload file is private operator input. Do not commit it, paste it, log it,
or summarize its raw contents. Local-operator apply still requires a prior
matching dry-run recorded by Launchplane.

Unsupported secret source shapes must fail closed in caller guidance. Do not
translate committed secret references, provider env lookups, stdin/stdout
transport, arbitrary secret ids, or "reuse current value" requests into a
product-config request.

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

Unauthorized, unavailable, denied, stale, and mismatched-intent responses keep
the same envelope and include compact `summary.error_code`, `summary.trace_id`,
and `warnings` entries. They do not include raw request bodies.
