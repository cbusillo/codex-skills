# Vendored Agent/Operator Contract

`agent-operator-contract.json` is the public-safe Launchplane contract consumed
by this skill. Its current identity is:

- schema version: `1`
- normalization version: `1`
- semantic digest: `6dfd9bfb019169fc2493f5304443424f4573ee7426ca52e796e11e3eacb1b693`
- non-gating source provenance: `7aa6360486e3a20c3c5f6b052041fc90ae39cd46`
- operation count: `20`

This artifact is byte-identical to the published
[Launchplane contract at 3cbc59e4](https://github.com/cbusillo/launchplane/blob/3cbc59e48003a3b0822f3c702f8ac38dcd4210b4/contracts/agent-operator-contract.json).
It includes the terminal enrollment, private credential claim, ordinary session
and finite-job operations. The embedded source provenance records the exporter
checkout; the published commit above identifies this retrieved artifact. Neither
provenance nor a matching contract grants runtime authority. Private claims are
consumed only by the private ordinary-agent adapter, outside the generic
agent-visible operator helper.

Relative to the previously vendored 12-operation artifact, this published
artifact adds eight ordinary-agent operations: enrollment proposal and status,
receiver-bound credential claim, session proposal/status/cancellation, and
finite-job admission/status. These additions introduce the
`terminal_agent_client`, `ordinary_agent_client`, and `private_agent_client`
surfaces, plus the `read_terminal_enrollment_requester`,
`read_ordinary_agent_proof`, and `read_ordinary_agent_receiver_claim` identity
dependencies. Five existing schema fingerprints also changed:
`apply_change_impact_policy`, `read_change_impact_policy`,
`reconcile_managed_authz_policy`, `read_governance_projection`, and
`write_merge_train_controller_run_once`. For those existing operations, the
route, method, modes, idempotency, reviewed evidence, supported surface, and
identity dependency fields are unchanged. The invariants and protected
workflow bindings are unchanged. Existing operator consumers remain covered by
their helper and contract tests. The bearer-identity contract for
`reconcile_managed_authz_policy` remains unchanged.

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

Run the advisory remote comparison locally with:

```bash
uv run launchplane/scripts/check-agent-operator-contract-freshness.py compare
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
Manual dispatch is compare-only unless issue reporting is explicitly selected.
`unknown` is retryable when the provider or transport is unavailable and never
creates a drift issue. The workflow is advisory maintenance evidence only: it
does not grant runtime authority or change helper permissions, and it must not
block ordinary Launchplane helper reads.

The merge-train policy import dry-run/apply commands and generic-web
deploy-recovery dry-run/apply commands are currently bounded local extensions
because they are consumed by `launchplane-write-action.py` but are not present
in the upstream public operation projection. The validator keeps all four explicit
and fails if an upstream artifact later projects the same routes, forcing a
deliberate migration instead of silently maintaining two sources of truth.

The helper also tracks one internal read-before-write route,
`GET /v1/work-graph/merge-train/policy-targets`, outside the projected command
count. Contract validation checks that this route remains absent from the
upstream projection so any future adoption requires an explicit migration.
