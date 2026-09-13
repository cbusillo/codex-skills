# Private ordinary-agent client

Read this when using ordinary delegation from stock CLI or a compatible host.
Run `scripts/launchplane-ordinary-agent.py`; the importable
`launchplane_ordinary_agent_client` module uses the same implementation. Routes
come from the [published contract](agent-operator-contract.md). The file store
supports macOS and Linux/POSIX with `flock`; unsupported platforms fail with a
bounded error.

## Connection and identity

Supply the approved service URL with `--url`. HTTPS is required except for
loopback HTTP rehearsals. Private state is bound to the normalized service URL
and origin; another URL needs a separate state directory. No redirect is followed.

The host privately supplies its existing terminal identity through
`LAUNCHPLANE_TERMINAL_CREDENTIAL` for enrollment proposal/status only. Credential
claim uses the saved receiver proof; session/job operations use the privately
claimed ordinary credential. The client never substitutes an owner, operator,
provider, browser or Actions credential. A denial is an authority or setup
prerequisite, not a reason to select a stronger identity.

State defaults to `$XDG_STATE_HOME/launchplane/ordinary-agent` when configured,
otherwise `~/Library/Application Support/Launchplane/ordinary-agent` on macOS
or `~/.local/state/launchplane/ordinary-agent` on Linux. Override it with an
absolute `--state-dir` or `LAUNCHPLANE_ORDINARY_AGENT_STATE_DIR`. Repository-local,
relative, symlinked or non-private state is rejected. Keep this directory private;
do not print its contents or include it in issue/PR artifacts.

## Commands

`--alias` selects a saved enrollment, session or job using a local label, not a
service ID. Each collection remembers its current selection. New requests default
to their stable retry key as the label; status and resume use saved selections.

| Command | Input and result |
| --- | --- |
| `enroll-propose` | Private intent file for a new enrollment; omit it to resume saved bytes. Returns public status and a validated service review link. |
| `enroll-status` | Reads the selected enrollment using terminal identity. |
| `claim` | Claims the selected approved enrollment, saves the credential, then prints only `{"status":"ready"}`. |
| `session-propose` | Private session intent file; saves original request and returned operation handle separately. |
| `session-status` | Reads the selected session; the initial session issued with the active claimed enrollment needs no second proposal. |
| `session-cancel` | Cancels the selected session; a valid acknowledgement need not contain lease selectors. |
| `job-admit` | Private finite intent file for a new job; omit it to resume exact saved admission bytes. |
| `job-status` | Reads the selected job through LP using its returned request handle. |

From the skill directory:

```bash
uv run scripts/launchplane-ordinary-agent.py --url <service-url> session-status
uv run scripts/launchplane-ordinary-agent.py --url <service-url> job-admit --input-file <private-intent.json> --alias qualification
uv run scripts/launchplane-ordinary-agent.py --url <service-url> job-admit --alias qualification
uv run scripts/launchplane-ordinary-agent.py --url <service-url> job-status --alias qualification
```

Input files must be user-owned private JSON (`0600`) containing bounded intent.
Enrollment supplies the supported descriptor, stable operation retry key, action,
principal/target, approved App and managed binding IDs, credential validity,
optional session attenuation (or `null`) and `delivery: {"expires_at": ...}`.
The client supplies the receiver digest. Session intent contains `descriptor_id`,
`operation_id` and `attenuation`. Use the service contract for shapes and existing
setup evidence for values; never place credentials/proofs in inputs or arguments.

Qualification intent needs `purpose: "qualification"` and `idempotency_key`.
Guarded delivery additionally supplies current `base_sha`, `pull_requests`,
`permitted_stack_edit_pull_requests` and `refresh_allowance`. Obtain intent from
the task and current provider/service evidence; the human need not type IDs or
hashes. Do not supply `session_id` or `lease_id`. The client selects returned
`preflight` or `guarded_merge` handles. LP owns scope, PR charging, eligibility,
expiry, admission and the actual merge.

## Interrupted work and output

The client saves its 32-byte receiver proof, canonical unpadded base64url encoding
and exact enrollment bytes before network activity. The claim digest uses the
prefix `launchplane:ordinary-agent-claim:v1`, NUL, then the proof's ASCII bytes.
Original retry keys remain separate from canonical service operation IDs. Claim
sends only Bearer proof, no body/query, and requires `Cache-Control: no-store`.

Commands hold a bounded process lock across state, network and response handling.
Atomic `0600` replacement and file/directory sync preserve prepared requests and
claimed credentials. `private_state_busy` means another command owns the store;
retry the same command after it finishes. Transport defaults to three attempts
and ten seconds per request. No background worker or polling loop starts.

Resume ambiguous enrollment or job admission with the same alias and no input
file. A lost response does not justify a new key. New admission reads the current
session and rejects known expired/revoked leases; exact replay uses saved request
bytes. Changed job intent with an existing alias is rejected; a new intended job
needs a new alias/retry key. Session proposal retries need the original intent.

Errors return a bounded JSON code and exit 2 without response bodies or secrets.
Normal status output contains public service metadata; claim output contains no
credential. Importing hosts must also keep state/proof APIs within their private
boundary. Client availability and contract conformance do not establish live
authority or qualification: approval, custody, policy, activation and worker start
remain service-owned prerequisites. The client cannot approve its own proposal
or silently renew an expired session.
