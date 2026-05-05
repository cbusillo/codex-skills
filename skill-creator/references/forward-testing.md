# Forward-Testing Skills

Use forward-testing to stress test a tricky skill with minimal leaked context. Treat subagents as a validation surface: the goal is to learn whether the skill generalizes, not whether another agent can reconstruct your intended fix from hints.

## Prompt Shape

Subagents should not know they are testing a skill. Treat them as agents asked to perform a realistic task.

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
- Ask for approval first when forward-testing may take a long time, require additional user approvals, or modify live production systems.
- In those cases, show the proposed prompt and request a yes/no decision plus any suggested modifications.

## Hygiene

- Use fresh threads for independent passes.
- Pass the skill and a request in a way similar to how the user would.
- Pass raw artifacts, not your conclusions.
- Avoid leaking expected answers or intended fixes.
- Rebuild context from source artifacts after each iteration.
- Review the subagent's reasoning, output, and emitted artifacts.
- Clean up artifacts between iterations when later agents could otherwise find them.

If forward-testing only succeeds when subagents see leaked context, tighten the skill or the test setup before trusting the result.
