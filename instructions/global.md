# Repository instructions

- Before repository work, read the Director's overall `DIRECTION.md` in `OWNER/direction` when configured, then the repository's `DIRECTION.md` if present, `AGENTS.md`, and relevant sections of `README.md`. Read nested `AGENTS.md` files before working in their paths; in repositories that still have `CLAUDE.md` or other legacy instruction files, read those too.
- Read relevant repository instruction files explicitly if the host has not supplied them.

# GitHub Branch Discipline

- For GitHub-backed repos, assume the default branch and shared/release/production branches are protected no-direct-work zones unless the user explicitly says otherwise.
- Before editing or committing, identify the current branch and repo default branch. If currently on the default branch, create and switch to a focused task branch first.
- Push only task branches for implementation work and open or update a PR. Do not attempt direct pushes to the default branch as a probe; branch protection failures are avoidable noise, not useful discovery.

## Direction sessions

- When a decision is the Director's to make, ask it as a question: say what is being decided, what changes on yes, and the recommended answer, together. Never end on a bare "say yes" or a context-free list.
- Separate what the Director must do by hand (sessions the agent cannot type into) from what the agent does on a yes.
- When something was already discussed and the Director says it is basically approved, treat that as the decision and act; do not ask again.

## Claims and defects

- Treat what the Director says, and your own earlier statements, as claims to check against evidence or an authoritative source before building on them; disagree plainly when the evidence contradicts them, and never agree just to agree.
- When a tool, helper, or skill from a repository the Director controls is defective, route it to that repository: with posting authority for that repository, search its issues and add to an existing one or file one; otherwise put the proposed issue in your report. A local workaround note is temporary and links the tracking issue once one exists.

## Tests

- Keep a test only if it fails when the product breaks and passes when someone makes an intended change.
- Never assert a literal defined elsewhere, such as a version, toolchain, hash, policy id, or instruction wording. Assert agreement with the one source of truth, or assert nothing.
- Never assert workflow or config text. Enforce the rule where it runs: in the workflow, in a helper with its own tests, or in a linter.
- Verification code must not depend on working-tree state such as untracked files or the host's installed runtime. Read tracked files or fixtures.
- Byte-exact and hash gates belong only on real artifacts and immutable evidence.
