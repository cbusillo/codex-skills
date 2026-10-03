# Vendored Agent/Admin Contract

`agent-operator-contract.json` is the public-safe Launchplane contract consumed
by this skill. Its current identity is:

- schema version: `1`
- normalization version: `1`
- semantic digest: `09a2898eb22c1aa2f25054b4f5d43755b05190a35f19c63cb11e0c2a6e31fd05`
- non-gating source provenance: `1fa6aa122374dff65ba26195f1cd3bfcf77839a7`
- operation count: `19`

This artifact is byte-identical to the published
[Launchplane contract at ce4f7f32](https://github.com/cbusillo/launchplane/blob/ce4f7f32f0ff766f9226903775d3928ccb195de1/contracts/agent-operator-contract.json).
It includes the terminal enrollment, private credential claim, ordinary session
and finite-job operations. The embedded source provenance records the exporter
checkout; the published commit above identifies this retrieved artifact. Neither
provenance nor a matching contract grants runtime authority. Private claims are
consumed only by the private ordinary-agent adapter, outside the generic
agent-visible admin helper.

This refresh removes the retired change-impact policy operations and projects
`apply_odoo_addon_settings`, which was previously a local helper extension.
The product-config, governance and product-retirement schema fingerprints also
changed, and stable-lane repair now binds its reusable workflow. Local Odoo
private-file, secret-binding, reviewed-plan and idempotency controls remain in
place. Contract freshness does not establish deployed runtime compatibility.

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
or updates one maintenance issue through the maintained GitHub issue helpers.
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
`scripts/launchplane_contract.py` (named in the SKILL.md Agent/Admin Contract
section), plus the private Client-review reader,
are currently bounded local extensions because they are consumed by local helpers but are not present
in the upstream public operation projection. The validator keeps these explicit
and fails if an upstream artifact later projects the same routes, forcing a
deliberate migration instead of silently maintaining two sources of truth.

The helper also tracks three internal routes outside the projected command count:
the read-before-write `GET /v1/work-graph/merge-train/policy-targets`, the
read-back `GET /v1/dokploy-targets/inspect`, and the read-back
`GET /v1/private-health-endpoints/records/{endpoint_key}`. Contract validation checks that
these routes remain absent from the
upstream projection so any future adoption requires an explicit migration.
