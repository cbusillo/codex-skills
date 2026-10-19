# Vendored Agent/Admin Contract

`agent-operator-contract.json` is the public-safe Launchplane contract consumed
by this skill. It is retrieved byte-for-byte from Launchplane's published
`contracts/agent-operator-contract.json`; the vendoring commit names the
Launchplane commit it came from. The artifact itself owns its identity:
schema and normalization versions, semantic digest, source provenance and
operations. The conformance gate below prints them. Neither provenance nor a
matching contract grants runtime authority. Contract freshness does not establish deployed runtime compatibility.

See the [private ingress caller contract](write-action-helper-contract.md#application-testing-private-ingress)
for its installed service API bindings.

Run the offline conformance gate from the `launchplane` skill directory with:

```bash
uv run scripts/check-agent-operator-contract.py
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

Run the advisory remote comparison locally with:

```bash
uv run scripts/check-agent-operator-contract-freshness.py compare
```

The separate `Launchplane Contract Freshness` workflow runs the same comparison
on a weekly schedule and through manual dispatch. It reports exactly three
classifications:

- `current`: the validated upstream and vendored semantic digests match;
- `known-stale`: the upstream artifact is valid and the semantic digests differ;
- `unknown`: transport, decoding, schema, normalization, or comparison evidence
  is insufficient.

Provenance-only changes remain `current`. A scheduled `known-stale` result opens
one workflow-authored maintenance issue or posts refreshed evidence as a
workflow-authored comment on the existing issue. The reporter accepts the
original request inside the canonical GitHub planning preservation envelope,
using the planning helper's parser, and leaves the request and managed plan
unchanged. Malformed envelopes and foreign authors fail closed. Comment updates
read back both the comment's author and evidence and the issue's ownership.
The scheduled reporter uses only the job's existing GitHub Actions token and
issue permissions, scoped to this workflow on `main`, and reads its write back.
It does not use `/user` (unavailable to workflow installation tokens), load local
credentials, edit another actor's issue, or retry an ambiguous write. Local
reporting continues through the maintained automation identity helpers.
Manual dispatch is compare-only unless issue reporting is explicitly selected.
`unknown` is retryable when the provider or transport is unavailable and never
creates a drift issue. The workflow is advisory maintenance evidence only: it
does not grant runtime authority or change helper permissions, and it must not
block ordinary Launchplane helper reads.

The commands listed under `LOCAL_EXTENSION_ROUTES` in
`scripts/launchplane_contract.py`, plus the private Client-review reader, are
bounded local extensions: local helpers consume them, but the upstream public
operation projection does not contain them. The validator fails if an upstream
artifact later projects the same routes, forcing a deliberate migration instead
of silently maintaining two sources of truth.

Every projected operation must be covered exactly once: by a projected helper
command, a protected workflow, the read-only governance projection, or
Launchplane's producer-owned service/UI/CLI surface. The validator's
operation coverage in `scripts/launchplane_contract.py` owns that
classification; a newly projected operation fails the gate until it is
assigned deliberately.

For the producer-owned private testing activation and transport planner, use
Launchplane's `docs/private-testing-transport.md` at the qualified service
revision. Contract coverage does not add a catalog activation command or grant
permission to activate a listener. The endpoint helper's public CA support is
described in the [private endpoint contract](write-action-helper-contract.md#private-health-endpoints).

The helper also tracks internal routes outside the projected command count:
the read-before-write `GET /v1/work-graph/merge-train/policy-targets`, the
read-back `GET /v1/dokploy-targets/inspect`, and the read-back
`GET /v1/private-health-endpoints/records/{endpoint_key}`, and the ingress planner's
`GET /v1/edge-endpoints/records/{endpoint_key}`, scoped absence confirmation via
`GET /v1/route-bindings/records`, and applied-ingress audit readback via
`GET /v1/ingress/route-audits/records/{record_id}`. The two inert domain-authority
and ingress planners have local-extension bindings with a dry-run-only
conformance check. Contract validation checks that
these routes remain absent from the
upstream projection so any future adoption requires an explicit migration.
