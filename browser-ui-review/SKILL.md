---
name: browser-ui-review
description: Use a real browser to open pages, click, type, scroll, inspect visible UI state, and capture screenshots when evidence is useful. Use when a task depends on interacting with a webpage instead of guessing from code or HTML alone.
---

# Browser UI Review

Use this skill whenever the task requires a live browser session.

## Primary tool

Use the global `ui-browser` helper instead of ad hoc HTML inspection or one-off screenshots.

- For multi-step tasks, prefer one Bash block with a named session variable such as `session="browser-$RANDOM"`, then pass `--session "$session"` to every `ui-browser` command in that block.
- Start or reuse a session with `ui-browser open <url>`.
- Keep the same session alive while you inspect and interact.
- Close the session with `ui-browser close` when you are done.
- Do not rely on the shared default session when parallel agents or background tasks may also be using the browser helper.

## Common commands

- `ui-browser open <url> [wait_ms]`
- `ui-browser click <selector> [after_wait_ms]`
- `ui-browser fill <selector> <text>`
- `ui-browser type <selector> <text>`
- `ui-browser press <key> [after_wait_ms]`
- `ui-browser select <selector> <value>`
- `ui-browser wait <ms>`
- `ui-browser wait-for <selector> [timeout_ms]`
- `ui-browser scroll <dx> <dy>`
- `ui-browser scroll-to <selector>`
- `ui-browser snapshot`
- `ui-browser text <selector>`
- `ui-browser exists <selector>`
- `ui-browser eval <expression>`
- `ui-browser screenshot <output-path>`

## Default workflow

1. Open the requested page with `ui-browser open <url>`.
2. Wait for app-specific readiness with `ui-browser wait-for ...` instead of guessing from source code.
3. Run `ui-browser snapshot` before the first interaction so selectors and visible state are grounded in the browser, not guessed from source.
4. Interact with the page using `click`, `fill`, `type`, `press`, `select`, `scroll`, or `eval` as needed.
5. Re-run `ui-browser snapshot` after navigation, modal/menu open or close, tab changes, or any click that substantially changes the UI.
6. Capture a screenshot only when it adds evidence or the user asked for an artifact.
7. Close the session when the task is complete.

## Interaction loop

Use a snapshot-driven loop for multi-step work:

```bash
session="browser-$RANDOM"
ui-browser open "https://example.com" --session "$session"
ui-browser wait-for "text=Ready" --session "$session"
ui-browser snapshot --session "$session"
ui-browser click "role=button[name='Continue']" --session "$session"
ui-browser snapshot --session "$session"
```

Snapshot again after:

- page navigation or route changes
- opening or closing modals, menus, popovers, tabs, drawers, or accordions
- submitting forms
- filtering/sorting/searching data
- toggling modes or settings
- any command that fails because the selector or element reference appears stale

Treat stale selectors as normal browser state drift. Recover by taking a fresh
snapshot and choosing the next selector from the new visible state instead of
forcing the previous selector through `eval`.

## Selector guidance

- Prefer stable Playwright selectors such as `text=`, `role=`, labels, placeholders, or specific CSS selectors.
- If a click changes the page, inspect the result with `ui-browser snapshot` or `ui-browser text ...` before continuing.
- Use `ui-browser exists <selector>` to confirm conditional UI before branching.
- Prefer user-facing selectors (`role=`, label text, placeholder text, visible
  text) for signoff interactions. Use CSS selectors when the user-facing target
  is ambiguous or when checking layout-only details.
- Avoid `ui-browser eval` for normal user-flow signoff. Use it for diagnostics,
  measuring layout, or inspecting state that is not otherwise visible.

## UI QA inventory

For UI review, frontend implementation, or bug verification, write a brief QA
inventory before final signoff:

- user-visible claims or requirements you are about to verify
- primary controls and modes that should work
- important states to inspect, including at least one post-interaction state
- viewport or device sizes that matter for the task
- screenshots or text evidence you expect to capture

Functional checks and visual checks are separate. A clicked path working does
not prove the UI is visually acceptable; a screenshot looking plausible does not
prove the controls work. Cover both when the task involves user-facing UI.

## Design handoff validation

If a `handoff*.md` file or returned design notes exist, use them as the visual
QA source of truth instead of inventing a new style direction.

- Extract acceptance criteria, required states, responsive requirements, and
  implementation constraints into the QA inventory.
- Compare the live browser result against the accepted design direction, not
  just generic visual quality checks.
- Check every required state from the handoff that can reasonably be reached:
  initial, empty, loading, error, dense data, success/completed, and
  mobile/narrow.
- If implementation differs from the handoff, classify the difference as an
  intentional product/technical tradeoff, missing implementation, infeasible
  design output, or visual quality issue.
- Do not sign off until screenshots support the accepted direction or the
  tradeoffs have been explicitly accepted.
- After the handoff has been consumed, remove the handoff file unless the user
  asks to keep it for another iteration. If durable decisions came from the
  handoff, migrate those decisions into the relevant plan or repo docs before
  removing the transient file.

## Visual checks

- Inspect the initial viewport before scrolling.
- Verify the state the user actually cares about, not only the empty or loading state.
- For app-like shells, dashboards, editors, games, and tools, confirm required controls and the primary interactive surface fit without unintended clipping.
- For scrollable pages, confirm the initial viewport communicates the core experience and exposes the expected starting action or context.
- Look for clipping, overflow, illegible text, weak contrast, broken layering, layout jumps, awkward spacing, and controls that are present but hard to perceive.
- If motion or transitions matter, inspect at least one transition or animated state in addition to the settled state.
- When a screenshot and a metric disagree, trust the visible defect and investigate rather than letting numeric checks overrule the screenshot.

Useful diagnostic checks through `ui-browser eval`:

```javascript
({
  innerWidth: window.innerWidth,
  innerHeight: window.innerHeight,
  scrollWidth: document.documentElement.scrollWidth,
  scrollHeight: document.documentElement.scrollHeight,
  canScrollX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  canScrollY: document.documentElement.scrollHeight > document.documentElement.clientHeight,
})
```

## Expectations

- Default to browser interaction, not screenshot-only behavior.
- When you expect multiple commands, keep the workflow inside one Bash invocation so the named session variable stays in scope for every browser step.
- Avoid substitutes like raw `curl`, static HTML reading, or OS screenshots when a live browser session is the right tool.
- Keep screenshots as supporting evidence, not the whole workflow.
- Save screenshot artifacts exactly where the user requested. If no path was specified, prefer `scratch/ui-checks/`.
- Use descriptive screenshot names that include the surface and state, for example `scratch/ui-checks/home-initial.png` or `scratch/ui-checks/settings-error-state.png`.
- `ui-browser screenshot` validates PNG output and retries once if the capture is blank or background-only.
- The legacy `ui-capture <url> <output-path> [wait_ms]` helper still exists, but only for pure one-shot capture tasks.
