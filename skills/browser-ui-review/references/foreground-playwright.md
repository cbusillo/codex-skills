# Foreground Playwright

Read when no suitable host browser interface is available, a background browser
helper cannot retain its page, and Playwright plus its browser are already
installed. Use the maintained Playwright API directly in a task-owned foreground
Node process. Do not install a browser, revive a retired controller, or edit
unattributed installed helper files to obtain this path.

## Keep the journey in one process

Inspect the selected project's package manifest and resolve its existing
Playwright module. If it is outside the project, pass the verified module's
absolute ESM entry path through `PLAYWRIGHT_MODULE`; keep machine-specific paths
out of committed examples. Node does not resolve a bare module name from an
unrelated installation automatically.

Write a temporary task script and execute it in one foreground shell invocation
on either harness. Await the entire journey; do not detach the browser with
`nohup`, launch it in one tool call and assume it survives another, or use a
fresh browser for each step. A persistent host browser interface is preferable
when the next interaction depends on a later tool call or a person's answer.

Adapt this example to the requested URL and accessible controls. Readiness and
navigation targets come from live visible state, not guessed product selectors.
The screenshot path must follow the host's artifact policy.

```javascript
// Task-owned .mjs script; use the existing project import if available.
import { pathToFileURL } from 'node:url';
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  const page = await context.newPage();
  await page.goto(process.env.BROWSER_URL, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: 'Ready', exact: true }).waitFor();
  console.log({ url: page.url(), text: await page.locator('body').innerText() });
  await page.getByRole('link', { name: 'Continue', exact: true }).click();
  await page.getByRole('heading', { name: 'Details', exact: true }).waitFor();
  console.log({ url: page.url(), text: await page.locator('body').innerText() });
  await page.screenshot({ path: process.env.BROWSER_SCREENSHOT, animations: 'disabled' });
} finally {
  await browser.close();
}
```

Run the script with `node /path/to/task-script.mjs`, keeping the browser and
context task-owned. The `finally` closes only that launched browser, including
when a locator, navigation or screenshot fails. Do not attach to or close other
sessions. An interrupted process is not a cleanup receipt; verify owned process
exit and artifacts before reporting completion.

## Evidence and limits

Use the entry point's QA inventory and visible interaction workflow. For
read-only observation of an installed app, constrain requests to the authorized
reads before opening it; do not click a write action merely to prove navigation.
Record the URL and visible state before and after navigation using the same
page. A screenshot file alone is insufficient: inspect the image and reject a
blank/background-only capture. Preserve any selected interface's existing
blank-image guard; a substitute does not make a rejected capture pass.

Report this as foreground Playwright evidence, with its actual browser and
viewport. It proves that journey on that interface, not repaired daemon
acceptance, an existing signed-in profile, or another browser/device. Keep the
daemon failure and implementation ownership unresolved until tracked source
and a reproduction establish them.
