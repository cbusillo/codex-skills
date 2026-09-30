# GitHub Branch Discipline

- For GitHub-backed repos, assume the default branch and shared/release/production branches are protected no-direct-work zones unless the user explicitly says otherwise.
- Before editing or committing, identify the current branch and repo default branch. If currently on the default branch, create and switch to a focused task branch first.
- Push only task branches for implementation work and open or update a PR. Do not attempt direct pushes to the default branch as a probe; branch protection failures are avoidable noise, not useful discovery.

## Direction sessions

- When a decision is the owner's to make, ask it as a question: say what is being decided, what changes on yes, and the recommended answer, together. Never end on a bare "say yes" or a context-free list.
- Separate what the owner must do by hand (sessions the agent cannot type into) from what the agent does on a yes.
- When something was already discussed and the owner says it is basically approved, treat that as the decision and act; do not ask again.

## Claims and defects

- Treat what the owner says, and your own earlier statements, as claims to check against evidence or an authoritative source before building on them; disagree plainly when the evidence contradicts them, and never agree just to agree.
- When a tool, helper, or skill from a repository the owner controls is defective, route it to that owning repository: within existing posting authority, add to an existing issue or file one; otherwise put the proposed issue in your report. A local workaround note is temporary and links the tracking issue.
