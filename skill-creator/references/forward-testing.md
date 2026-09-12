# Forward-Testing Skills

Use forward-testing to stress test a tricky skill with minimal leaked context. Treat subagents as a validation surface: the goal is to learn whether the skill generalizes, not whether another agent can reconstruct your intended fix from hints.

## Prompt Shape

For execution cases, give fresh agents a realistic task without the expected
answer, diagnosis, or intended fix. For source review, explicitly request a
review and label its result as source-review evidence. These test different
things; a review prompt is not an execution test.

Good:

```text
Use $skill-x at /path/to/skill-x to solve problem y.
```

Avoid:

```text
Review the skill at /path/to/skill-x; pretend a user asks you to...
```

## Decision Rule

- Err on the side of forward-testing substantial or fragile skills.
- Apply [task scope and authorization](../../references/execution-scope.md).
  Reuse existing approval for the action, scope, and cost. Prepare ordinary cases
  with isolated fixtures and bounded execution.
- Ask only when additional authority, consequential cost, or a reserved live
  target is needed. Complete independent authorized preparation first and make
  the proposed test concrete and reviewable.
- Select the host and evidence type using [validation guidance](validation.md).

## Hygiene

- Use fresh threads for independent passes.
- Pass the skill and a request in a way similar to how the user would.
- Pass raw artifacts, not your conclusions.
- Avoid leaking expected answers or intended fixes.
- Rebuild context from source artifacts after each iteration.
- Review the subagent's actions, output, and emitted artifacts.
- Clean up artifacts between iterations when later agents could otherwise find them.

If forward-testing only succeeds when subagents see leaked context, tighten the skill or the test setup before trusting the result.
