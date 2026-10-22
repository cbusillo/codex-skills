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
Subsequent replacement allocation and replacement-qualified record callers
remain separate consumer work on [codex-skills#1308](https://github.com/cbusillo/codex-skills/issues/1308).
