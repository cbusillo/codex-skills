# Stable Deploy Identity

Read before preparing or repairing an application-target deployment.

For a Dokploy application target, the product build workflow must publish both
an immutable image digest and an immutable SHA tag in the same repository before
requesting deployment. Send the digest-pinned `repository@sha256:digest` value
as `artifact_id` and the published `repository:sha-<commit>` tag as
`deploy_reference`. Launchplane keeps `artifact_id` as the evidence and runtime
artifact identity, uses `deploy_reference` only for the provider-facing Dokploy
image, and records that provider tag as `image_reference` for read-back evidence.

Treat non-floating tags as an agent-side safety requirement even if a current
validator does not recognize every branch-shaped tag. Never use `latest`, a
branch name, an environment name, or another floating tag as
`deploy_reference`. This two-reference requirement is specific to application
targets; resolve the target category from Launchplane context or the supported
operator surface rather than product workflow inputs or checked-in metadata.

If the SHA tag was not published or the deploy payload omitted
`deploy_reference`, repair the product repository's build/deploy workflow and
publish a new immutable pair. Do not edit Dokploy or Launchplane runtime records
directly. Delegate protected workflow dispatch and watching to the `github`
skill.

