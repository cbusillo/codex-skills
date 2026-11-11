# Producer contract and local caller bindings

The [Launchplane producer contract](https://github.com/cbusillo/launchplane/blob/main/contracts/agent-operator-contract.json)
is the source for operation modes, supported surfaces, reviewed evidence,
idempotency and protected-workflow bindings. Read it when selecting a surface or changing a caller. The catalog keeps no
copy and makes no whole-contract freshness claim.

`scripts/launchplane_contract.py` holds only this client's installed HTTP
bindings. `HELPER_OPERATIONS` maps installed commands to operation ids, and
`OPERATION_PATHS` supplies their shared paths;
`LOCAL_EXTENSION_ROUTES` and `INTERNAL_HELPER_ROUTES` supply the other installed
bindings. These are implementation details, not permission or a complete
inventory of producer operations. Changes to the producer alone do not install
a new catalog command. The helper's `--help` lists installed commands; their
payload, evidence and readback behavior lives in the
[write-action helper contract](write-action-helper-contract.md).

Run the offline local-binding check after route changes:

```bash
uv run scripts/check-agent-operator-contract.py
```

It checks local route shape, command resolution and the dry-run-only planner
bindings. It does not fetch the producer or prove deployed compatibility.
Payload, target binding, reviewed evidence, idempotency, redaction and readback
checks remain in the individual callers and `launchplane_safety.py`; run the
affected helper safety modules when changing those callers.

For a protected workflow, resolve its binding from the producer and delegate
dispatch and watching to the GitHub skill's `github_workflow_babysit.py`.

For private testing activation and transport planning, read the producer's
`docs/private-testing-transport.md`. For
installed disposable-lane inspect, collect, record, clear and prepare-rebuild commands, read the
[disposable lane contract](write-action-helper-contract.md#disposable-lane-observations).
Record supports the retained-operation link with all facts unknown. For reviewed
replacement allocation and actual resource qualification, read the
[replacement resource caller contract](write-action-helper-contract.md#empty-testing-replacement-resources).
These consumers use the producer's existing scope and original-operation guards;
their installation proves no live workflow outcome.

## Generic caller

`launchplane-call.py` reads the configured service's `/openapi.json` on every
invocation, including its `x-launchplane-agent-contract.operations` extension.
It resolves the operation ID, method, path and safe response fields from that
live description. Only operations whose producer-declared `supported_surfaces`
includes `generic_caller` use this path; the service qualifies each operation
before its legacy wrapper is removed. The caller has no per-operation request
checks or response field lists.
Product-config uses this caller; the other installed wrappers remain in place.

```sh
uv run scripts/launchplane-call.py apply_product_config --mode dry-run \
  --body-file /private/request.json \
  --response-file /private/task-evidence/example/review.json

uv run scripts/launchplane-call.py apply_product_config --mode apply \
  --body-file /private/request.json --idempotency-key example-apply \
  --response-file /private/task-evidence/example/apply.json
```

Use the existing private operator configuration above. The body file stays
outside Git; response files must be new, under an existing task-evidence
folder, and are created with mode 0600 before any request. Read the saved dry
run privately before applying the same intent. Supply any reviewed digest or
review object the service requires in the body itself. `--mode` sets the body's
top-level mode; omit it when the operation embeds its mode elsewhere.
Use repeated `--path name=value` and `--query name=value` for parameters.

The caller prints HTTP/service status, trace ID, the service's error or refusal,
and scalar fields marked `x-launchplane-safe-output: true`. A marked container
does not authorize its contents; unmarked, unknown and unsupported schema
branches stay private. The complete operation response goes to the named
private file. Its status is the service's answer, not a separate qualification.
On transport loss or an HTTP 408/5xx apply response, the outcome is unknown.
A malformed reply is unverified. Retain the original body, evidence and apply
key and reconcile the original operation before retrying. The caller never retries or replans.

Launchplane owns request validation, authority, matching dry-run enforcement,
idempotency and operation-specific recovery. Read its live request schema and
[operation docs](https://github.com/cbusillo/launchplane/blob/main/docs/agent-operator-contract.md)
for the selected operation. For ordinary configuration, read back with a fresh
dry run showing no planned changes. First-empty uses `product-secret-bindings-read`;
business hours uses `product-profile-read`. Recorded settings do not prove
runtime sync or deployment.
