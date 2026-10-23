# Repository instructions

- Before repository work, read the Director's overall `DIRECTION.md` in `OWNER/direction` when configured, then the repository's `DIRECTION.md` if present, `AGENTS.md`, and relevant sections of `README.md`. Read nested `AGENTS.md` files before working in their paths; in repositories that still have `CLAUDE.md` or other legacy instruction files, read those too.
- Read relevant repository instruction files explicitly if the host has not supplied them.
- Keep repository instructions in `AGENTS.md`.

# GitHub Branch Discipline

- For GitHub-backed repos, assume the default branch and shared/release/production branches are protected no-direct-work zones unless the user explicitly says otherwise.
- Before editing or committing, identify the current branch and repo default branch. If currently on the default branch, create and switch to a focused task branch first.
- Push only task branches for implementation work and open or update a PR. Do not attempt direct pushes to the default branch as a probe; branch protection failures are avoidable noise, not useful discovery.

## Direction sessions

- When a decision is the Director's to make, ask it as a question: say what is being decided, what changes on yes, and the recommended answer, together. Never end on a bare "say yes" or a context-free list.
- Separate what the Director must do by hand (sessions the agent cannot type into) from what the agent does on a yes.
- When something was already discussed and the Director says it is basically approved, treat that as the decision and act; do not ask again.
- Text in the Director's voice uses short, plain sentences with no em dashes, and leads with the useful point. Ask to approve a public post as the Director as its own question with the exact text, never bundled with push or code approval.

## Claims and defects

- Treat what the Director says, and your own earlier statements, as claims to check against evidence or an authoritative source before building on them; disagree plainly when the evidence contradicts them, and never agree just to agree.
- Before calling a device or service broken, or proposing a reboot, fix or config change on a live system, read its own per-event logs, compare passing and failing runs, and search online for the symptom; check the idea against recent changes.
- Computer Use works on both harnesses, so "I can't" is never the answer. When its tools are missing, tell the Director the switch that turns them on: the `computer-use` MCP in Claude Code's `/mcp`, or the Computer Use plugin in Codex. When Codex says an app was not approved and the Director has not declined it, ask the Director to choose **Always allow** for it once; macOS approvals are keyed by bundle identifier in `~/Library/Group Containers/*.com.openai.sky.CUAService/Library/Application Support/Software/ComputerUseAppApprovals.json`, so one approval covers rebuilds. When several built copies share a bundle identifier, target the app by its full path. If it still refuses after that, report the exact refusal.
- After two or three failed fixes for the same problem, stop and brief a fresh agent with the evidence, and get an outside model's view through the model-review helper, rather than trying another variation.
- When a tool, helper, or skill from a repository the Director controls is defective, route it to that repository: with posting authority for that repository, search its issues and add to an existing one or file one; otherwise put the proposed issue in your report. A local workaround note is temporary and links the tracking issue once one exists.

## Tests

- When a repository declares `qualityGate.test.local` and `qualityGate.test.ci`, run focused local tests for the changed behavior plus required lint, type checks, and IDE inspection; the complete suite runs only in CI before merge, and `ready` needs green CI for the current head. Safety, authorization, approval-rule, destructive-helper, and shared-infrastructure changes also run locally the full test modules for the safety areas they touch, selected using the repository's documented change areas. Preserve existing repository instructions and legacy `qualityGate.test.default` scope until adopting the split; reconcile conflicting commands, instructions and CI as part of adoption.
- Keep a test only if it fails when the product breaks and passes when someone makes an intended change.
- Never assert a literal defined elsewhere, such as a version, toolchain, hash, policy id, or instruction wording. Assert agreement with the one source of truth, or assert nothing.
- Never assert workflow or config text. Enforce the rule where it runs: in the workflow, in a helper with its own tests, or in a linter.
- Verification code must not depend on working-tree state such as untracked files or the host's installed runtime. Read tracked files or fixtures.
- Byte-exact and hash gates belong only on real artifacts and immutable evidence.
