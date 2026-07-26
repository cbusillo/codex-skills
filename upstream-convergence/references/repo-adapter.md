# Repository Adapter

Use this reference after the skill finds `upstream/convergence-policy.json`.
The manifest is an identity and discovery boundary, not a generic policy
language.

## Version 1

```json
{
  "schemaVersion": 1,
  "upstream": {
    "repository": "OWNER/REPO",
    "remote": "upstream",
    "branch": "main",
    "allowedFetchUrls": [
      "https://github.com/OWNER/REPO.git",
      "git@github.com:OWNER/REPO.git"
    ]
  },
  "contractsPath": "upstream/convergence-contracts.md",
  "evidenceRoot": "upstream/OWNER-REPO",
  "planIssue": "https://github.com/OWNER/FORK/issues/123"
}
```

Reject unknown schema versions, unknown fields, absolute paths, parent-path
segments, backslash paths, symlink escapes, empty URL lists, and malformed
repository identities. Do not add arbitrary executable hooks, lane patterns,
contract IDs, branch mutation, credentials, or build commands to the manifest.

## Driver Convention

The repository-owned entrypoint is:

```sh
python3 .github/scripts/upstream_convergence.py <phase>
```

Inspect that file from the trusted starting revision before running it. The
driver supports exactly these responsibilities:

| Phase | Writes | Required behavior |
| --- | --- | --- |
| `inspect` | No | Resolve exact refs, verify remote identity and ancestry, calculate conflicts/residuals, and report governance changes. |
| `record` | One new evidence directory | Require a clean isolated task worktree, append atomically, and refuse overwrite or changing `HEAD`. |
| `validate` | No | Check adapter/governance wiring, guards/waivers, snapshots, historical mutation, and bounded provenance. |

The driver must not fetch, merge, resolve conflicts, build, commit, push, open a
PR, alter worktrees, clean state, or advance an ownership baseline. Those steps
remain visible orchestration decisions.

## Missing Adapter

Without an adapter, perform only read-only discovery. Do not guess the upstream
repository, remote, product contracts, or evidence paths. If the repository is
an intentional maintained fork, propose the minimal manifest and repository
driver as a focused implementation; otherwise use normal Git workflows.
