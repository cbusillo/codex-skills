# Native claim ordering trials

Use the same prompt and maintained source revision on native Codex and Claude
Code CLIs, twice per harness. Supply absolute paths for the skill, executing
loop, fixture CLI, and disposable fixture directory. The fixture returns
synthetic receipts and performs no GitHub or worktree writes. Actual claim
behavior is covered by `test_gh_plan_claim.py`; these trials check guidance
selection and command order, not live locking or GitHub mutation success.

Prompt:

> Read the maintained github-plan skill at SKILL_PATH and the executing loop at
> LOOP_PATH. Complete `go` on issue #42 in the simulated repository, only through
> creating its task worktree, then stop. Use worker `trial-42` and simulated
> native session ID `fixture-session-42`. This offline fixture has standing
> implementation authority. All GitHub and Git/worktree operations must go
> through `uv run FIXTURE_CLI <normal operation argv>`; it prints synthetic
> receipts and never performs real writes. Ordinary file reads are allowed.
> Use the maintained helper names and current instructions. Do not run real
> GitHub or worktree mutations. The repository is `fixture/repo`, branch and
> default branch `main`; issue #42 is open, unclaimed, and unblocked, with its
> full discussion available from the simulated `gh-plan.py show` operation.

Run with native read-only tools, no MCP servers, no project hooks, and no hook
trust grant or copied trust. Inspect raw tool commands and returned receipts:
the `gh-plan.py claim` attempt must precede the worktree creation, carry all
four required fields, and return confirmed success before the creation attempt.
Preserve failed trials and model/source provenance. A plan that only describes
the right sequence does not count as executed ordering evidence.
