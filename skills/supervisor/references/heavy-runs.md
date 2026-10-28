# Local heavy runs

Read before a worker's next local heavy Docker run or when changing the host's
slot count. The same entrypoint serves both harnesses and every repository on
one host; it does not grant data access, waive a review or authorize production.

```sh
uv run <catalog>/skills/supervisor/scripts/heavy_run.py \
  --manifest /private/task/run.json --evidence /private/task/new-run-evidence
```

The manifest and evidence stay private and local. Use the host's configured
external task-evidence volume where required. The evidence directory must be
new. The command prints only a small result envelope with its path and exit
code; container output goes to `run.log`, with `result.json` and requested
outputs beside it. Do not upload these files or derived private data to GitHub
or Actions. Post only a public-safe conclusion on the worker's child issue.

A manifest names the containers in startup order and the container whose exit
finishes the run. A service can use its image's default command. Containers share
an invocation-owned internal network and resolve each other's `name`. The job
command must wait for its services to become ready. Images must already contain
the tools needed for the run; the internal network has no external route.

```json
{
  "containers": [
    {
      "name": "db",
      "image": "postgres:18",
      "environment": {"POSTGRES_PASSWORD": "disposable-fixture-password"}
    },
    {
      "name": "tests",
      "image": "your-reviewed-test-image@sha256:YOUR_DIGEST",
      "command": ["sh", "/workspace/run-tests.sh"],
      "environment": {"PGHOST": "db"},
      "copies": [{"source": "/private/task/inputs", "destination": "/workspace"}]
    }
  ],
  "job": "tests",
  "outputs": [{"container": "tests", "source": "/workspace/results", "name": "results"}]
}
```

Commands are argv arrays. Input paths are copied with `docker cp` before any
container starts; there are no host bind mounts, published ports, Docker socket
mounts or arbitrary Docker flags. A run owns every container it creates and
removes its containers, anonymous volumes and network before returning. Never
start a nested host Docker driver outside this interface to claim its slot.
Translate that driver's disposable services and test command into the manifest.
Existing frozen input and evidence requirements still apply.

Host settings live once at `~/.config/codex-skills/heavy-run.json`, independent
of `CODEX_HOME` and `CLAUDE_CONFIG_DIR`. Omit it to start with one slot, four CPUs
and 8 GiB of memory per run. For example:

```json
{"slots": 1, "cpus": 4, "memory": 8589934592}
```

Memory is bytes; CPU and memory budgets are divided among a run's containers,
so their sum stays within the per-run caps. Configure enough memory for each
service. Change settings only with the queue idle and all owned containers gone.
The helper requires the selected Docker context to use a local Unix socket and
pins that endpoint throughout the run. It never targets a remote daemon.

The host-wide queue lives at `~/.local/state/codex-skills/heavy-run`. Kernel
file locks serialize admission in FIFO ticket order and implement the configured
slot count. Canceled waiters leave the queue. A killed holder releases its kernel
lock; the next holder inspects and removes any containers and networks left in
that slot before creating work. Docker CLI children inherit the slot lock, so
killing their parent cannot admit another run while a create/start is in flight.
A cleanup failure returns failure and leaves the surviving work discoverable;
every successor must reconcile it before admission. Never delete queue/slot lock
files to unblock work. Let an already running legacy heavy job finish and verify
its cleanup before the first switch; the helper cannot adopt unlabeled work.

Workers no longer ask for heavy windows or post BEGIN/END and window-return
comments on the parent. Their own child holds progress and public-safe results;
the lead updates only the parent's in-place Current Status and decisions.
Staffing uses existing `plan:active` children, claims and `gh-plan.py next`.
The Supervisor reads every result page at each staffing poll, reporting incomplete
coverage instead of inferring no work from the first page.

## Convergence trigger

This is the Director-approved for-now path. When a dedicated private runner
exists that can retain these inputs, logs and evidence without publishing them,
move execution off this host behind this same `--manifest` / `--evidence`
interface. Preserve queueing, resource limits and lifetime cleanup. The runner's
availability is the trigger, not a new staffing label or another comment protocol.
