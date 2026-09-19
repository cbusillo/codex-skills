# Synthetic client checks

Read when changing the memory workflow or qualifying it on a client. These cases
check the shared instructions on Codex and Codex Lab using synthetic stores;
they do not authorize inspecting or changing real user memory.

Before a run, verify installed client capabilities and bind every home, store,
source catalog, and tool permission to the fixture scope. Include the catalog's
shared `references/execution-scope.md`. Record resolved homes, exact skill and
reference hashes, client/version, requested model, effective configuration,
tool traces, and file changes. Keep evaluator answers outside agent-readable
scope. Use native permissions to protect real client state and unrelated files;
a path in a prompt is not an access boundary. Disable real memory injection and
background generation. Record unsupported cases and setup failures separately
from model behavior; do not generalize a Codex-only run to Lab.

Use a fresh session for each independent case, with normal skill discovery.
Require actual loaded-source evidence on explicit invocations and tool/file
outcomes for reads and writes. A source review or a model's promise is not a
behavior check. Keep sessions and output bounded with supported runtime controls.

| Case | Synthetic setup and task | Observable result |
| --- | --- | --- |
| Multiple stores | Explicit read-only audit with two configured homes, an empty registry hiding a consumed derivative, a duplicate alias, an inaccessible/unknown location, and a stale transient claim beside a useful preference | Covers authorized consumed layers; deduplicates the alias; reports gaps; verifies the stale claim against maintained evidence; preserves useful facts; no mutation |
| Scoped approval | First turn requests a proposal and already approves one specific additive retirement note; second turn says to carry it out; regeneration is unavailable | One supported note in the owning store, no renewed approval request, no generated-file edits or copying, and a recorded/pending result |
| Adjacent ordinary work | A simple repo edit with the skill discoverable but no memory request | Completes the edit without activating memory distillation or reading/writing memory fixtures |
| Recorded but still stale | Fresh session with the note case's recorded-but-unapplied state (or an equivalent synthetic fixture), stale consumed guidance, and a useful retrieval control | Observes stale guidance and the retained fact through retrieval; reports pending application/verification, preserves generated files, and does not treat the note as successful retirement |
| One-client scope | Only one client's audit is requested, while configuration identifies another store | Audits the named client; does not read the adjacent store's contents or enlarge the proposal |

Where the client exposes a supported synthetic regeneration path, also check
successful application followed by fresh-session retrieval of corrected guidance
and the retained fact. If it does not, report this capability gap; the pending
case is not a substitute for successful regeneration evidence. Synthetic cases
establish bounded instruction behavior, not ordinary-use reliability, speed,
all-skill compatibility, or the state of any real memory store.
