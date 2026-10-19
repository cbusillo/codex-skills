# Test quality warnings

`scripts/check_test_quality.py` scans tracked test files for likely change
detectors. Run it with Python 3.12 or newer; it has no third-party dependencies.
It prints candidates for review and exits zero even when it finds them.
An unreadable repository or input exits nonzero; a Python parse error becomes
a visible coverage warning. Do not hide scanner failures with `continue-on-error`.

```sh
uv run scripts/check_test_quality.py --repo . --format github
uv run scripts/test_check_test_quality.py
```

The five rules come from the recorded
[scan](https://github.com/cbusillo/codex-skills/issues/1692#issuecomment-6113532065):

1. Tracked config values pinned to string literals: hard-failure candidates.
   Config includes `.github/**`, `pyproject.toml`, lockfiles, Dockerfiles,
   compose YAML, entitlements, Info.plist and package.json.
2. Tracked workflows loaded without observable execution: warnings.
3. Literals asserted against tracked source text or `inspect.getsource`:
   hard-failure candidates.
4. Asserted version-shaped tokens also found in tracked non-test files:
   hard-failure candidates. This includes three-part versions, `>=` specifiers
   and action `@vN` references.
5. Catalog-only string assertions against SKILL.md, AGENTS.md, references
   Markdown or `instructions/` Markdown: hard-failure candidates. Catalog identity is recognized by the
   tracked scanner and review-reference paths together.

All classifications still emit warnings and exit zero during this trial.
Ordinary CLI/error sentence assertions are behavior and stay quiet.

Python uses syntax analysis and simple aliases for file reads and assertions,
excluding recognizable temporary-directory paths, tracked fixture directories,
and execution outputs. Data flow stops at opaque product calls; custom readers
must expose a tracked path argument to be recognized.
Swift and JavaScript/TypeScript use bounded call and alias patterns for
config/duplicate-version checks and workflow warnings, including multiline
calls. The scanner selects
test directories, Python `test_`/`_test` files, Swift `Tests`/`Test` files and
JavaScript `.test`/`.spec` files. It reads their current tracked working copies;
untracked files are excluded.

This is a heuristic, not proof that the assertion is wrong. It cannot follow
arbitrary data flow or understand every language/test DSL. Foreign-language
source-text/instruction checks, computed paths, imported loaders with no path
argument, and custom assertion helpers can escape it. Python execution
detection follows `self` and `cls` method calls within the same class. A
`setUp` or `setUpClass` fixture counts as executed only when a class method
that runs a process also reads an attribute the fixture stored, whether or not
that attribute holds the workflow text. An unrelated execution in the same
test, or a helper method replaced at run time, can still hide a workflow
warning, and a step run through a module-level helper function is still a
false positive. Foreign alias
detection is file-wide and conservative.
Coincidental version matches in dependency locks or documentation need triage;
token duplication alone does not prove ownership.
Rule 1 covers equality/value pins; raw config membership checks outside workflow
loads are not classified by it. Regex assertions, pytest fixture return values,
class constants and more complex paths/data flow need later tuning. Very deep
alias/expression chains and foreign-language regex literals are also outside
the bounded parser's reliable coverage. Keep this trial nonblocking.
Comments and Python string fixtures are ignored. Source-of-truth agreement,
temporary fixtures and real artifact/immutable-evidence checks remain valid;
triage candidates using the repository's test rubric. Symlinked files and
binary/non-UTF-8 files, gitlinks and locally deleted files are excluded; the
report does not prove complete coverage.
No blocking mode is enabled: tune false positives before introducing one.

## Wire another repository's CI

1. Check out the consumer repository as usual, then check out this catalog at
   a reviewed full commit SHA into `.test-quality-catalog`. Use a second
   `actions/checkout` step; do not copy the scanner into the consumer. Updating
   the SHA is a deliberate catalog update, not a test literal to assert. The
   extra checkout is an untracked directory: run the scanner after other
   linters/clean-tree checks, or add `.test-quality-catalog/` to
   `.git/info/exclude` and exclude it from any recursive consumer linter.
2. Install Python 3.12 or newer using the consumer's existing setup. Invoke the
   scanner with the consumer root as `--repo`; the script's location does not
   select the repository. If using uv, use `uv run --no-project --no-config` so
   the consumer's dependency setup cannot affect this dependency-free script.
3. Add the warning step to every CI path that validates tests, including the
   merge-train candidate path. Keep the consumer's existing runner selection
   and fork safety. Do not mark warning findings as failures or add this as a
   new required status check during this trial.

```yaml
- name: Check out test quality scanner
  uses: actions/checkout@<consumer-approved-full-action-sha>
  with:
    repository: cbusillo/codex-skills
    ref: <reviewed-full-catalog-commit-sha>
    path: .test-quality-catalog
    persist-credentials: false
- name: Warn about change-detector tests
  run: python3 .test-quality-catalog/scripts/check_test_quality.py --repo . --format github
```

4. Run the same command locally and triage each finding. If a whole fixture
   file is deliberately excluded, use repeatable `--exclude 'path/glob'` and
   record the reason beside the CI command. Prefer fixing detection over broad
   exclusions. Use `--format json` for scan counts and examples.
5. Prove the invocation on a temporary PR with a tracked synthetic test such as
   `assert product.version == "4.5.6"` and a tracked non-test file defining
   that version: its line should receive a warning and
   the step should succeed. Replace it with `assert product.version ==
   metadata.version`: that warning should disappear. Remove the probe before
   landing. No tests should inspect or assert the consumer's workflow text.

Launchplane and context-panel need their own CI integration PRs using these
steps. The guard PR here does not change those repositories. In this catalog,
the existing validation workflow invokes the local scanner directly.
