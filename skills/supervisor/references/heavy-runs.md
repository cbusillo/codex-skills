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
finishes the run. Services keep their image's default command and working
directory. Containers share
an invocation-owned internal network and resolve each other's `name`. The job
command must wait for its services to become ready. Images must already contain
the tools needed for the run and be cached locally; `--pull never` prevents an
implicit image pull under the slot. Prepare images through the repository's
supported image path first. The internal network has no external route.

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

Commands are argv arrays. Environment values use private mode-0600 env files,
not process arguments, and are removed after create. For multiline configuration,
copy a configuration file. Inputs stream through `docker cp` before any container
starts. The archive makes container-local copies readable and writable by image
users, including services that drop privileges; files retain executable intent.
Copied directories are mode 0777, regular files 0666 and executable files 0777;
use a dedicated workspace destination, not `/` or a system directory such as
`/tmp`. For strict-mode files or data directories, prepare new files under the
image user's ownership and required modes inside the job before using them.
Non-root outputs belong inside the copied workspace; automatically created
parents of nested destinations remain root-owned. Original host bytes and
private permissions stay unchanged. Keep exported outputs inside the private
evidence directory; their container modes are preserved. There are no host
bind mounts, published ports, Docker socket mounts or arbitrary Docker flags. A run owns every container it creates and
removes its containers, anonymous volumes and network before returning. Never
start a nested host Docker driver outside this interface to claim its slot.
Translate that driver's disposable services and test command into the manifest.
Existing frozen input and evidence requirements still apply.

Host settings live once at `~/.config/codex-skills/heavy-run.json`, independent
of `HOME`, `CODEX_HOME` and `CLAUDE_CONFIG_DIR`: the helper resolves the OS
account's actual home directory. Omit it to start with one slot, four CPUs
and 8 GiB of memory per run. For example:

```json
{"slots": 1, "cpus": 4, "memory": 8589934592}
```

Memory is bytes; CPU and memory budgets are divided among a run's containers,
so their sum stays within the per-run caps. CPUs round down to whole
nano-CPUs; every container needs at least 0.01 CPUs and 6 MiB. Configure enough
memory for each service. Change settings only with the queue idle and all owned containers gone.
The helper reads Docker's context from the OS account's Docker configuration,
ignoring `DOCKER_*` overrides. It requires a local Unix socket and stores that
endpoint with custody. Recovery uses the predecessor's endpoint even if the
selected local context has since changed. It never targets a remote daemon.

The host-wide queue lives at `~/.local/state/codex-skills/heavy-run`. Kernel
file locks serialize admission in FIFO ticket order and implement the configured
slot count. Resource labels also bind the queue identity, isolating other state
roots on the same daemon. Canceled waiters leave the queue. A suspended waiter still owns its FIFO
position; resume or cancel that same waiter to advance it. A killed holder releases its kernel
lock; the next holder inspects and removes any containers and networks left in
that slot before creating work. Docker CLI children inherit the slot lock, so
killing their parent cannot admit another run while a create/start is in flight.
Recovery logs go to the predecessor's evidence directory when it still exists,
never to another run's log. Missing predecessor evidence means logs are dropped.
A cleanup failure returns failure and leaves custody with the original run;
every successor to that slot must reconcile it before admission. There is no
background reaper: an orphan keeps its capped resources until that slot's next
holder cleans it up. Admission tries the lowest free slot first; its failed
recovery can keep later requests from using other free slots until recovery
succeeds. Never delete queue/slot lock files to unblock work. Use `lsof` on the
exact ticket or slot lock file to identify its holder, then resume/cancel that
worker through its harness. If the stored endpoint is unavailable, restore that
same local daemon through the host's supported service path, then retry the
entrypoint. Unreachability alone never proves its containers are gone; preserve
custody and report the unknown state while restoration is blocked. For a verified orphaned CLI, stop that exact PID under
[process cleanup](../../work-closeout/SKILL.md#git-and-worktree-hygiene). Let an
already running legacy heavy job finish and verify its cleanup before the first switch; the helper cannot adopt unlabeled work.

Follow the [worker reporting rules](brief-rules.md#rules-every-brief-carries)
and [Supervisor staffing procedure](../SKILL.md#the-pattern).

## Convergence trigger

This is the Director-approved for-now path. When a dedicated private runner
exists that can retain these inputs, logs and evidence without publishing them,
move execution off this host behind this same `--manifest` / `--evidence`
interface. Preserve queueing, resource limits and lifetime cleanup. The runner's
availability is the trigger, not a new staffing label or another comment protocol.

An agent can set `executor` to a command argv array in the existing host
settings file. The same entrypoint validates the manifest, then replaces its
process with that adapter and absolute `--manifest` / `--evidence` paths.
It creates no local queue or Docker resources and has no local fallback if the
adapter fails. Process replacement preserves signal delivery and exit status.
Omit `executor` for local execution. Configure it only after existing local
running, queued and unreconciled work has drained.

The private adapter owns transport, runner identity, architecture/provenance,
remote cancellation, private evidence retrieval and the runner's single queue.
Its reviewed operations runbook lives in the configured private infrastructure
repository. Do not put private endpoints or credentials in catalog settings
examples. Inspect the original remote job after a connection loss before retrying;
an adapter failure never proves remote cleanup.

The executor setting routes every heavy run from that OS account on the host,
including non-Odoo repositories; verify its consumers before a cut-over.
Execution limits come from the destination host's settings. Workstation limits
are used only for workstation execution.

Private adapters can use `run(..., prepare=..., complete=...)` from the same
helper. Preparation runs after admission and predecessor reconciliation;
evidence delivery runs after cleanup and receipt creation, while the same slot
is still held. Keep all transfer children within that process and preserve the
slot descriptor for Docker operations. This keeps staging and collection from
overlapping another execution without adding a scheduler.
