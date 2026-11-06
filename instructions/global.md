# Repository instructions

- Before repository work, read the Director's overall `DIRECTION.md` in `OWNER/direction` when configured, then the repository's `DIRECTION.md` if present, `AGENTS.md`, and relevant sections of `README.md`. Read nested `AGENTS.md` files before working in their paths; in repositories that still have `CLAUDE.md` or other legacy instruction files, read those too.
- Read relevant repository instruction files explicitly if the host has not supplied them.
- Keep repository instructions in `AGENTS.md`.
- Before writing Python or Odoo XML/JavaScript, read the [shared Python and Odoo style](https://github.com/cbusillo/codex-skills/blob/main/skills/references/python-odoo-style.md) in full. Apply framework-specific sections only to that framework and preserve the repository's supported Python versions.

# GitHub Branch Discipline

- For GitHub-backed repos, assume the default branch and shared/release/production branches are protected no-direct-work zones unless the user explicitly says otherwise.
- Before editing or committing, identify the current branch and repo default branch. If currently on the default branch, create and switch to a focused task branch first.
- Push only task branches for implementation work and open or update a PR. Do not attempt direct pushes to the default branch as a probe; branch protection failures are avoidable noise, not useful discovery.
- Create task worktrees through the catalog's `skills/github/scripts/dev-worktree` with its per-host configuration, so guarded retirement runs before creation. A configured host helper still enforces its storage and retirement policy.

## Direction sessions

- Before asking the Director a question, read the [shared question guidance](https://github.com/cbusillo/codex-skills/blob/main/skills/references/talking-with-the-director.md#director-questions).
- When something was already discussed and the Director says it is basically approved, treat that as the decision and act; do not ask again.
- Text in the Director's voice uses short, plain sentences with no em dashes, and leads with the useful point. Reply approval follows the [Client-reply rule](https://github.com/cbusillo/codex-skills/blob/main/skills/references/talking-with-the-director.md#client-replies).

## Claims and defects

- Treat what the Director says, and your own earlier statements, as claims to check against evidence or an authoritative source before building on them; disagree plainly when the evidence contradicts them, and never agree just to agree.
- Before calling a device or service broken, or proposing a reboot, fix or config change on a live system, read its own per-event logs, compare passing and failing runs, and search online for the symptom; check the idea against recent changes.
- Computer Use works on both harnesses, so "I can't" is never the answer. Prefer Codex's Computer Use, because it works in the background without taking over the Director's screen. Supervisor workers follow the brief rules' GUI relaunch route; other Claude Code sessions start Codex through the same [GUI launch](https://github.com/cbusillo/codex-skills/blob/main/skills/supervisor/references/helpers.md#opt-in-codex-gui-work): `skills/supervisor/scripts/iterm_tab.py new --account-provider openai --computer-use required`, with `GUI task: <bundle id>` as the brief's first line and a link to the brief rules. Never use `codex exec` for it; it refuses every app not already approved. Browser work from Claude Code uses the catalog Chrome route (`mcp__catalog-chrome__*`), not Codex. Use Claude's Computer Use only when Codex's tools cannot reach the app, and only in a visible session the Director approved for screen use; a refusal or a declined approval is never a reason to switch. Outside Supervisor workers, when its tools are missing, tell the Director the switch that turns them on: the `computer-use` MCP in Claude Code's `/mcp`, or the Computer Use plugin in Codex. When several built copies share a bundle identifier, target the app by its full path. If Codex still refuses an app, report the exact refusal; app approvals and macOS permissions stay the Director's.
- After two or three failed fixes for the same problem, stop and brief a fresh agent with the evidence, and get an outside model's view through the model-review helper, rather than trying another variation.
- When a tool, helper, or skill from a repository the Director controls is defective, route it to that repository: with posting authority for that repository, search its issues and add to an existing one or file one; otherwise put the proposed issue in your report. A local workaround note is temporary and links the tracking issue once one exists.

## Tests

- When a repository declares `qualityGate.test.local` and `qualityGate.test.ci`, run focused local tests for the changed behavior plus required lint, type checks, and IDE inspection; the complete suite runs only in CI before merge, and `ready` needs green CI for the current head. Safety, authorization, approval-rule, destructive-helper, and shared-infrastructure changes also run locally the full test modules for the safety areas they touch, selected using the repository's documented change areas. Preserve existing repository instructions and legacy `qualityGate.test.default` scope until adopting the split; reconcile conflicting commands, instructions and CI as part of adoption.
- Keep a test only if it fails when the product breaks and passes when someone makes an intended change.
- Never assert a literal defined elsewhere, such as a version, toolchain, hash, policy id, or instruction wording. Assert agreement with the one source of truth, or assert nothing.
- Never assert workflow or config text. Enforce the rule where it runs: in the workflow, in a helper with its own tests, or in a linter.
- Verification code must not depend on working-tree state such as untracked files or the host's installed runtime. Read tracked files or fixtures.
- Byte-exact and hash gates belong only on real artifacts and immutable evidence.
