# GitHub capability profile

Read this when installing the automation App, diagnosing an access refusal, or
adding a GitHub API surface. The existing [operation matrix](operation-matrix.toml)
is the source for required capabilities, permission level and scope, caller
role, safe probe, degraded behavior, and default-profile impact. It includes
maintained helpers and the explicitly supported families reached through the
API/token wrappers; an arbitrary passthrough endpoint is not automatically covered.

Run from the catalog root:

```bash
uv run skills/github/scripts/github-capabilities.py profile
uv run skills/github/scripts/github-capabilities.py profile --read-only
uv run skills/github/scripts/github-capabilities.py audit --repo OWNER/REPO
uv run skills/github/scripts/github-capabilities.py audit --all-installed
```

The default is the **full-operation** repository profile: Contents, Issues,
Pull requests, Actions, Discussions and Workflows write; Metadata, Checks,
Commit statuses, Administration, Deployments, Code scanning alerts, Dependabot
alerts and Secret scanning alerts read. Read-only is derived from that same
catalog, with write levels lowered and write-only Workflows omitted. No
organization or user grant is silently added to a repository installation.

Contents covers ordinary Git and release operations. Workflows is the additional
grant for changing workflow files, including through a branch update or merge.
Actions write supports authorized dispatch, rerun, cancellation and run/artifact
maintenance. Read access to runners/settings, deployments and security evidence
prevents diagnosis from stopping at an unexplained 403.

Keep these roles explicit:

- Ruleset/protection writes and full bypass-actor inspection use the verified
  owner through the existing ruleset helper. Administration read does not replace
  that writer evidence.
- Protected environment approvals retain the workflow helper's active human
  reviewer and exact environment authorization.
- Publishing authoritative Checks, Commit statuses and Deployment records uses
  the configured workflow/service identity and its declared job permissions.
- Projects depend on the Project owner and token type. Organization Apps can
  have organization Projects access; personal-token paths use `read:project`
  or `project`. Preserve the helper's explicit Project actor override and
  recoverable issue-first behavior; report unsupported owner/token combinations.
- Package registry authentication uses an authorized workflow `GITHUB_TOKEN`
  or a supported classic PAT with package access. Some Packages REST endpoints
  accept App tokens; verify the exact endpoint instead of treating that as
  registry authentication.

Permission availability and task authorization are different. A profile is not
permission to merge, deploy, approve an environment, publish, delete, or trust
every bot. Preserve DIRECTION, the current task grant, branch protection and
Launchplane's train policy. Existing authorization carries forward; do not ask
again for the same authorized scope merely because a new helper is involved.

## Interpreting an audit

The audit verifies the configured App/installation identity using JWT, inventories
its actual repository membership, and uses strict automation auth without human
fallback. `--all-installed` includes every installed repository; archived or
disabled repositories are explicitly excluded from operational probes. An
all-repositories installation covers future repositories under that account;
selected-repository installations need membership changes separately.

A granted write capability reports `permission_granted`, with `write_probe:
not_exercised`. Only safe reads are attempted. A normal authorized operation
supplies write execution proof. Workflows has no read-level permission and is
checked against accepted installation grants without a synthetic push. Discussions
uses a bounded GraphQL query. Secret scanning always goes through the maintained
redacted reader; neither literal secrets nor raw provider responses are emitted.

`permission_missing`, `not_installed`, `not_enabled`, `no_data`,
`requires_separate_actor`, and `unavailable` have different meanings. Successful
public reads are not proof of private access. An ambiguous 404 or a denial despite
a declared grant remains unavailable, never clean. A separate-actor result is a
known limit of this App audit, not a request to expand the agent's identity.
An `audited` report means collection completed, not that every capability passed.

When an authorized grant changes, update the App registration and accept the
installation update once, then rerun with `--refresh-token` to renew the **same**
App's cached token. Verify the affected private-repository reads and the next
normal authorized write. Before expanding a shared installation, inspect its
other consumers: a service that treats installation grants as an exact token
ceiling can break. Consumers should request and verify their own narrowly scoped
tokens while recognizing the installation may serve other approved roles.

GitHub App grants do not update Launchplane PR-author policy. Onboard each intended
train target through its supported service flow and include relevant bot roles
there. Dependabot, GitHub Actions, an agent App, and a deployment/check App do
different jobs. A shared bot login does not establish workflow provenance; admit
only the intended authors and retain ready labels, current CI and repository
scope. Do not extend a three-target author-policy edit to an entire fleet by
inference or grant merge rights to every installed App.

## Keeping the profile current

The normal catalog validation runs `validate-operation-matrix.py`. New public
commands need operation rows. Maintained GitHub helper API signatures, endpoint
strings and GraphQL documents are fingerprinted across the GitHub, rollup,
babysitter and direction helper directories. A new or changed surface fails
validation until its operation declaration, permission, safe probe, degraded
behavior and profile impact are reviewed together. Dynamic passthrough input
and arbitrary untracked code remain outside this static check.

After reviewing the change, print candidate hashes with `github-capabilities.py
fingerprints` and update the matrix's fingerprint table in the same PR. The
command never writes or blesses the catalog itself. Add a behavioral probe test
when a new capability/probe is introduced; do not satisfy drift by copying a hash
without inspecting the permission boundary. Keep real account/repository/App
identities and live audit reports in ignored operator state, not this catalog.

Primary references: [App permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps),
[installation token scope](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app),
[Discussions](https://docs.github.com/en/graphql/guides/using-the-graphql-api-for-discussions),
[Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects),
and [Packages](https://docs.github.com/en/packages/learn-github-packages/introduction-to-github-packages).
