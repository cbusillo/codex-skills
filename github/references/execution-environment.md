# Execution Environment Policy

Use this reference when adding or changing an executable dependency, runtime,
runner image, external CLI, installer source, or cross-repository workflow
route. It records which surfaces are pinned, which intentionally float, how
they are updated, and what validation prevents silent drift.

## Classified Surfaces

| Surface | Selection and mutability | Update path | Validation |
| --- | --- | --- | --- |
| Remote GitHub Actions | Approved publishers are GitHub-owned `actions/*` and trusted `astral-sh/setup-uv`. Publisher trust controls the allowlist, not immutability: every remote executable reference uses a full commit SHA with a release-tag comment. Local actions are exempt because they execute the checked-out commit. | Weekly Dependabot `github-actions` PRs. | `scripts/validate_github_actions_security.py` checks publishers, SHAs, provenance comments, and the required Dependabot entry. |
| PEP 723 direct dependencies | Exact repository-consistent pins. Transitive dependencies remain uv-resolved at execution time. | Weekly/manual `Update PEP 723 Dependencies` PR workflow. | `scripts/update_pep723_dependencies.py --check` plus the full repository gate. |
| uv executable | `uv.toml` declares the reviewed current pre-1.0 range `>=0.11.29,<1`, confirmed July 20, 2026. CI installs the newest release in that channel; operator workstations must meet the same floor. | `astral-sh/setup-uv` resolves the newest compatible uv from `uv.toml` on every CI run. Review the channel explicitly before admitting uv 1.x, and raise the floor when a newer audited baseline becomes repository policy. | Every PR, merge-train candidate, post-merge run, and weekly dependency workflow executes with the selected uv. Workflow validation rejects action-level version overrides so `uv.toml` remains authoritative, and the gate prints the exact uv version as a run receipt. |
| Python | Every PEP 723 script supports `>=3.12`. Python 3.12 is the minimum-compatibility and dependency-update lane; current stable Python 3.14 is a second validation lane. Patch releases intentionally float. Direct GitHub shell wrappers use an isolated `uv run --no-project --no-config --python 3.12` runtime unless a test/operator override is explicit, so a caller's project cannot change helper execution. | uv resolves/downloads the current patch for each lane. Add the next stable minor to current-version validation when upstream enters its bugfix phase; raise the minimum before 3.12 reaches upstream end of life. | `.python-version`, setup-uv matrix/input policy, wrapper guards, PEP 723 metadata validation, and the full gate must agree. Pull-request and merge-train validation require both 3.12 and 3.14 to pass, and each gate prints the selected patch version. |
| GitHub-hosted runner | Workflows pin the GA OS major `ubuntu-24.04`; GitHub's weekly image revisions intentionally float within that OS. Ubuntu 24.04 was reconfirmed against the runner-images GA table on July 20, 2026. | Review the runner-images GA table before moving to a newer Ubuntu LTS. Do not use preview images or move back to `ubuntu-latest` without a policy update. | The execution-environment validator checks every job's `runs-on`; every image revision must pass normal CI and merge-train validation. |
| Core workstation/runner CLIs | `bash`, `git`, `gh`, `jq`, and `node` are environment-provided compatibility surfaces rather than repo-installed packages. | GitHub updates hosted-runner tools; workstation operators manage local installations. A change that needs a newer CLI feature must verify the hosted image and document the new minimum here. | The canonical gate fails early if any core CLI is missing and prints each selected version. The full gate then exercises shell, Git, GitHub, jq/YAML, and Node-backed paths. |
| Host utilities | POSIX/macOS utilities such as `sh`, `awk`, `sed`, `grep`, `mktemp`, `curl`, and `open` follow the selected runner or operator OS rather than an independent repository version. | GitHub runner-image or workstation OS updates. Code that needs a non-portable option must add a capability check or replace the dependency. | Normal workflow and skill tests exercise required paths; platform-specific optional paths fail with an owning-skill diagnostic. |
| Optional operator CLIs | `gcloud`, `code`, `claude`, `lms`, JetBrains IDEs, and browser-control clients are invoked only by their owning optional skills. | Operator-managed. Each owning skill documents setup and capability checks. | Missing tools block only that optional workflow and must produce an explicit diagnostic; they are not prerequisites for the repository gate. |
| Skill installer GitHub refs | The installer currently accepts and defaults to mutable refs such as `main`. | Tracked remediation: issue #452 will resolve and report immutable commit SHAs across download and git transports. | Until #452 lands, a caller-supplied full SHA improves the direct download path but the repository does not guarantee identical provenance across transport fallback; reproducible installs remain a tracked gap. |
| Launchplane merge-train runner route | `.github/github.json` intentionally routes to mutable `cbusillo/launchplane` `main`; this first-party routing metadata is an explicit exception to immutable checked-in executable refs because it selects a remote controller rather than claiming a source revision. | Launchplane owns runner deployment and policy. | Each run records the resolved workflow head SHA; controller evidence separately records candidate and landing SHAs. The validator locks the route, while `revisionEvidenceFields` names the per-run audit fields rather than storing one run's SHA values in repository metadata. |

## Dependency Introduction Rule

Before adding any new versioned executable dependency or changing one in this
table:

1. Use the official project documentation or release source to identify the
   current supported stable/GA major or channel on the review date.
2. Classify the surface as one of: immutable with an automated update path,
   intentionally floating with continuous validation, operator-managed and
   optional, or a concrete tracked remediation gap.
3. Prefer a stable major/minor channel over `latest` when upstream can move the
   runtime across an OS or language boundary. Keep patch drift only when normal
   CI continuously exercises it.
4. Update this table, the owning validator, and focused tests in the same PR.
   A provenance comment, issue, or release note must explain how the selected
   major was confirmed.
5. Do not introduce a mutable installer or cross-repository execution ref
   unless the workflow records the resolved immutable revision or a linked plan
   issue owns that missing guarantee.

## Review Triggers

Review this policy when Python 3.12 changes upstream support phase, a new Ubuntu
LTS runner reaches GA, repository code requires a newer uv feature, a core CLI
feature raises its minimum version, or a new external execution surface is
added. Action and PEP 723 update automation do not replace those deliberate
runtime-major decisions.

## Primary Sources

- [setup-uv version and Python selection](https://github.com/astral-sh/setup-uv#usage)
- [uv `required-version` setting](https://docs.astral.sh/uv/reference/settings/#required-version)
- [Python version support status](https://devguide.python.org/versions/)
- [GitHub-hosted runner image labels and migration policy](https://github.com/actions/runner-images#available-images)
- [Dependabot configuration options](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference)
