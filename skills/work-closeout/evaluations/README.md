# Retired cleanup experiment

The Director retired the opt-in cleanup behavior experiment in
[Q1248](https://github.com/cbusillo/codex-skills/issues/3444#issuecomment-6634370757).
Its six helpers and five test modules are no longer maintained.

The [immutable experiment snapshot](https://github.com/cbusillo/codex-skills/tree/b990d02a4422cb6f6a74ebefe41a161c5fb78463/skills/work-closeout/evaluations)
keeps the method and its link to the September 13 acceptance evidence reachable.
The September 30 acceptance records in `evals/jb-concision` and
`evals/readiness-concision` retain their historical tool output unchanged.

The maintained cleanup inventory and preservation behavior remains covered by
[`test_repo_cleanup.py`](../scripts/test_repo_cleanup.py) and
[`test_cleanup_forks.py`](../scripts/test_cleanup_forks.py).
