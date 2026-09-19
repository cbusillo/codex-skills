# Review and Handoff

Use the shared formatting guidance in `../../references/every-code-formatting.md`
for issue, PR, readiness, and final-status writing.

## Independent Review Lanes

Select only lanes affected by the refresh:

- **Semantic contracts:** Does the merged implementation preserve each named
  product behavior rather than merely retaining files?
- **Security and provenance:** Are remote identity, exact refs, credentials,
  untrusted execution, path handling, and rollback boundaries sound?
- **Operations and release:** Do CI, packaging, signing, installation, updates,
  migrations, and rollback preserve repository-owned authority?

Each reviewer must receive the exact worktree path and commit. Discard a review
that inspected a different checkout or stale revision.

## Bounded Evidence

Record:

```text
State: <analysis complete | review required | safety refusal | validated>
Worktree: <real path>
Branch and HEAD: <branch> @ <full sha>
Range: <base> -> <upstream>, local <sha>
Provenance: <remote url, tracking ref, tracking tip>
Inventory: <conflict and residual counts by lane>
Contracts: <changed contract IDs and decisions>
Governance: <changed paths and verification result>
Guard: <guarded, violated, waived, stale counts>
Validation: <tests/builds/checks and exact result>
Publication: <PR, checks, merge or stacked-branch state>
Blocked by: <decision, credential, dependency, or none>
Next action: <one safe action>
```

Keep full path inventories in repository artifacts. Do not paste hundreds of
paths or waiver records into issue comments or terminal summaries.

Update the durable plan when the accepted upstream SHA, candidate head,
contract disposition, blocker, or publication state changes. Delegate issue
graph changes to `$github-plan`, PR execution to `$github`, readiness decisions
to `$repo-readiness`, and final cleanup to `$work-closeout` when those workflows
are triggered.
