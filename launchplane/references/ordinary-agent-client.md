# Portable ordinary-agent client

Use `scripts/launchplane-ordinary-agent.py` for the private ordinary-agent
lifecycle. The module behind it is importable for compatible hosts and resolves
all routes from operation IDs in `agent-operator-contract.json`.

The client creates one 32-byte receiver proof, encodes it as 43-character
unpadded base64url, computes the domain-separated
`launchplane:ordinary-agent-claim:v1` digest, and durably stores that proof
together with the exact enrollment request bytes before its first request. It
stores the service-returned canonical operation ID separately from the
enrollment request's retry key. The private claim request contains only the
proof as Bearer authorization, has no body or query, rejects redirects, and
requires `Cache-Control: no-store`; the claimed credential is durably stored
before the command reports readiness.

State is kept outside repositories. Set
`LAUNCHPLANE_ORDINARY_AGENT_STATE_DIR` or pass `--state-dir`; the default is an
OS-appropriate home state directory. The directory is owner-only and state is
atomically replaced with durable `0600` files. Session and job handles are
read from this state and service responses. The client selects a returned
lease by action and passes its ID opaquely; it never computes or asks the user
to type a lease or service request ID. It also never falls back to owner,
operator, provider, browser, or Actions credentials.

Enrollment input is a private `0600` JSON file containing nonsecret request
fields and `delivery: {"expires_at": ...}`. Session and finite-job input files
contain only their public bounded intent. Do not place credentials or receiver
proofs in these files or command arguments. Errors and command output contain
only bounded status and reason codes.

The vendored contract in this change is prepared from reviewed Launchplane
source for hermetic development. Its provenance does not establish deployment
or runtime activation; refresh it again from the confirmed landing artifact
before installation.

