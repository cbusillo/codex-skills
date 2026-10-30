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

## Native Docker lifecycle qualification

Use the [native Docker image recipe](../assets/native-docker/Dockerfile) when a
fixture must drive real Compose replacement, inspect/exec/stop/start, coherent
backup/restore or shared watched directories. It boots a disposable Linux VM
inside an ordinary job container using QEMU's userspace CPU emulation. The
existing manifest, caps, internal network, queue, executor and custody stay the
same. There is no privileged container, host socket, host mount, device access,
KVM, extra queue or helper option. No runner setting or artifact update is needed.

This recipe requires an AMD64 executor with cgroup v2 and a private cgroup
namespace. ARM execution is unqualified. Build the AMD64 image before submission:

```sh
docker build --platform linux/amd64 --tag native-docker-fixture \
  <catalog>/skills/supervisor/assets/native-docker
docker image inspect --format '{{.Id}}' native-docker-fixture
```

The configured executor's existing image/provenance transfer carries the cached
image; no registry credentials are forwarded. Use the resulting reviewed local
`sha256:...` image ID in a retained qualification manifest. A local build need
not have a registry digest. Base tags in the recipe are build
inputs, not immutable qualification evidence.

The guest contains Docker, Compose and Python. For additional fixture tools,
prepare a reviewed Alpine-based image and supply it as `--build-arg GUEST_IMAGE=...`.
Its initramfs root uses Docker's documented
[`DOCKER_RAMDISK` mode](https://docs.docker.com/reference/cli/dockerd/#environment-variables);
this proves native lifecycle behavior, not production root-filesystem parity.
Keep tools in that image and frozen source/data in the copied workspace.
The guest uses Docker's [containerd image store](https://docs.docker.com/engine/storage/containerd/)
on its disposable ext4 disk. Export cached nested images from a containerd store
with `docker image save --output images.tar REVIEWED_IMAGE`. Keep the original
index and its content when the fixture uses a repository@index-digest reference:
do not pass `--platform`, which replaces that index with a platform manifest.
All content needed by the export must be cached; an incomplete cache refuses
export. Prepare that original image again through its supported image path
before exporting; do not change the host's storage settings. This preparation
requires a host that already uses the containerd store. A platform-filtered
archive instead retains the platform-manifest
reference, not the original index. Classic-store archives cannot supply missing
original manifests. Do not recreate digest aliases or substitute a mutable tag.

The copied run script loads these archives with `docker load`, verifies the
required original repository@digest addresses with `docker image inspect`,
then runs the fixture with image pulls disabled. Inspect `RepoDigests` and the
created container's `Config.Image` against those original addresses. Importing
another target under the same tag replaces its earlier reference; prepare the
archive for the exact target the consumer requires. It uses the VM's Unix Docker
socket. Compose bind paths refer to the job's `/workspace` shared into the VM,
so the fixture and its containers can observe the same prepared/serving files.
Keep databases and writable mmap files on nested ext4 volumes, not the 9p
workspace share. All workspace writers run inside the guest; this does not
prove file locking or change notifications from an outer writer.
Check published HTTP ports from inside the VM: it has no network interface to
the outer job, runner or outside world. This does not qualify external ingress.

```json
{
  "containers": [{
    "name": "tests",
    "image": "sha256:REVIEWED_LOCAL_IMAGE_ID",
    "command": ["sh", "/workspace/run-native.sh"],
    "copies": [{"source": "/private/task/native-inputs", "destination": "/workspace"}]
  }],
  "job": "tests",
  "outputs": [{"container": "tests", "source": "/workspace/results", "name": "results"}]
}
```

Submit it with the same installed entrypoint:

```sh
uv run <catalog>/skills/supervisor/scripts/heavy_run.py \
  --manifest /private/task/native.json --evidence /private/task/new-native-evidence
```

The VM reserves memory for its outer job and derives vCPUs from that job's
existing cgroup limits. All its processes stay under those aggregate caps.
Its fresh ext4 Docker disk lives inside the disposable job; no nested daemon
state or volume survives outer-job removal. `HEAVY_VM_DISK_GIB` and
`HEAVY_VM_TIMEOUT_SECONDS` in the container environment select scratch capacity
and the bounded run deadline; defaults live in the image's [runner](../assets/native-docker/run.py).
Account for archives, source, filesystem caches, VM memory and output growth
before intake. The containerd store keeps both compressed image content and
unpacked snapshots on that disk. CPU emulation is slower than a bare host and
proves no production timing or capacity. The existing executor's headroom checks still apply.
Keep result files and verbose logs in `/workspace/results` before the command exits.
VM controls and the raw disk stay outside that copied/exported workspace. The image
returns the guest command's status and fails on missing completion evidence,
boot failure or timeout. Existing cancellation and predecessor recovery remove
the whole VM with its owning job; use the original executor request to collect
or cancel, never submit a replacement after unknown transport results.

This capability adds no global train, deploy or recovery gate. Only work already
using this heavy-run queue waits on its slot or unresolved custody. It removes
no existing gate. Long VM runs delay every repository using the same queue,
and shared storage I/O can delay neighboring host work; this adds no I/O ceiling.

The [native import regression](../scripts/fixtures/native-image-import.py) runs
as a copied workload through this same entrypoint. Copy it to `/workspace` with
the reviewed archives and an `images.json` array. Each entry supplies `tag`,
`archives` (workspace-relative filenames), `references` (original immutable
addresses expected after import), and `missing_references` (original addresses
omitted by that export). Run `python3 /workspace/native-image-import.py` as the
manifest command. Use small AMD64 images that provide `true`. Cover a cached
registry index and platform export, a complete local-build index export and a
filtered local-build export separately. The test checks real execution, `RepoDigests`,
`Config.Image` and refusal of missing content, without asserting daemon flags,
versions or configuration text.
This native workload is manual qualification, not an offline CI test.

## Host settings and queue

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

## Immutable directory overlays

An existing directory copy can opt into `"overlay": true`. On a local Linux
Docker engine this attaches a Docker local overlay volume at that copy's
absolute destination instead of copying the directory into the container.
The helper must run on the engine's Linux host with write access to its volume
directory. The private receiver runs it as root; membership in the Docker group
alone does not provide that filesystem access to a rootful engine.
There are no caller-supplied volume names, host mount flags or driver options.
Symlinks, special files, aliased lower paths, mount-option delimiters and
input destinations overlapping another copy are refused. Unsupported engines
fail explicitly; they never silently select the ordinary-copy path.

Containers in the same run with the same source and destination share their
writable layer. Each different destination or run gets a private upper/work in
a labeled backing volume. Lower data is read-only through the overlay. The
lower must already have permissions usable by the image user; the private
receiver normalizes newly transported overlay inputs just like container
copies, without changing host originals or existing retained inputs.
Local Linux callers can use `docker.stage_overlay_input(new_staged_directory)`
inside the existing queued `prepare` callback to apply the
[input-copy permissions](#local-heavy-runs). Use a fresh staging tree inside a
private enclosing directory, never a host original or retained lower.

Cleanup stops/removes containers, verifies lower content and filesystem
identity, removes the labeled overlay volumes before their backing volumes,
then removes the owned network. Recovery performs the same checks. A remaining
volume, failed unmount or changed lower retains cleanup custody and prevents a
successor. Writable file-store state is not exported into evidence automatically.
Normal outputs remain explicit. Omit `overlay` for the existing independent
copy behavior when a run needs its own base.

A private executor may resolve a copy's `retained_from` to an existing original
input under its own custody and frozen-transfer verification. It must preserve
local input/image bindings and the original request's retirement boundary. This
is not a host path supplied to the generic queue; local execution refuses that
field without an executor. The receiver's own runbook owns its reference schema,
installation and adoption. Do not add a second cache, queue or provenance system.
