# Vendored Agent/Operator Contract

`agent-operator-contract.json` is the public-safe Launchplane contract consumed
by this skill. Its current identity is:

- schema version: `1`
- normalization version: `1`
- semantic digest: `5ca368e08c9d1d094eba3ed5cf47a789b144bb81e6ff8722138440ebaff23b64`
- non-gating source provenance: `3f8e22ff1762be0b81d5eeb19f237a14b6a8bd4f`

Run the offline conformance gate with:

```bash
uv run launchplane/scripts/check-agent-operator-contract.py
```

The validator recomputes the digest from `normalization_version` and `contract`
only. A provenance-only source SHA change therefore does not create semantic
drift. It also rejects unsupported versions, malformed shapes, unsafe public
values, incomplete operation coverage, protected-workflow mismatches, and local
consumer bindings that no longer match the vendored contract.

A green result proves only that the checked-in artifact and this skill's local
helper, workflow, and invariant expectations are internally consistent. It does
not contact Launchplane and does not prove that the vendored artifact is current
upstream.

The generic-web deploy-recovery dry-run and apply routes are currently bounded
local extensions because they are consumed by `launchplane-write-action.py` but
are not present in the upstream 12-operation projection. The validator keeps
them explicit and fails if an upstream artifact later projects the same routes,
forcing a deliberate migration instead of silently maintaining two sources of
truth.
