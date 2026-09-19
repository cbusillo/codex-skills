# ui-browser Helper

Read this reference only when the installed `ui-browser` CLI is the selected
interface. Check its help before relying on the command shapes below; do not
install or restore Every Code to obtain a browser controller.

## Session ownership

Use an installed, working `ui-browser` helper only when it is the selected browser interface. It is independent of the retired Every Code agent.

- For multi-step tasks, prefer one Bash block with a named session variable such as `session="browser-$RANDOM"`, then pass `--session "$session"` to every `ui-browser` command in that block.
- Start or reuse a session with `ui-browser open <url>`.
- Keep the same session alive while you inspect and interact.
- Close only the named session created for this task with `ui-browser close`; preserve user-owned sessions.
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
7. Close the named session created for this task when the task is complete.

For release, runtime, browser-specific, or device-specific blockers, prefer
evidence from the affected browser/device when it is available. If you must
validate with a substitute environment, record the substitute and the remaining
gap in the browser QA evidence handed to `repo-readiness` or the owning PR,
issue, or plan instead of presenting it as equivalent proof.

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


`ui-browser screenshot` validates PNG output and retries once if capture is
blank or background-only. The separate `ui-capture` helper is only for pure
one-shot capture tasks when that helper is available.
