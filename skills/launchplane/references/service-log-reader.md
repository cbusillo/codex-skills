# Launchplane service logs

Use this reader first when diagnosing a Launchplane operation, especially a
lost response, timeout or replacement of the service. It reads Launchplane's
own retained API or worker container logs using an **existing** SSH identity
and read path. It makes no Launchplane API call, provider mutation or remote
file write, and never creates access. The separate tracked-target log API is
for product lanes; this helper needs no product grant or service endpoint.

Find the host's existing read path and self-service selectors in its private
operations docs through `docs-lookup`. Verify that the selected Compose project,
service and image repository identify Launchplane itself. Keep those selectors
in an explicit private JSON file, outside source control. There is no checked-in
target default, credential input, SSH configuration change or automatic discovery
from product records. A fake example:

```json
{
  "ssh_host": "existing-ops-alias",
  "lxc_id": 123,
  "compose_project": "example-control-plane",
  "compose_service": "api",
  "image_repository": "registry.example.test/control-plane"
}
```

`ssh_host` may be an existing SSH alias or `user@host`; it must use the identity
already documented for this read. Omit `lxc_id` for a direct Docker host. When
present it selects `pct exec <id> -- docker ...` through the same existing SSH
path. Select a worker by its exact Compose service. The helper lists running
and stopped retained containers with exact project/service label filters and
requires every selected image's original container configuration to match the
configured repository, even if a mutable tag has moved. A bounded inspect
projection rechecks the labels and reads only image identity and lifecycle
fields, never environment values. It refuses
unknown host keys and password prompts; it never enrolls a key or changes SSH
configuration. If existing access fails, preserve the failure and continue
source work. Repair a tool's incorrect read refusal under overall direction;
never change identity, add a grant or dispatch a workflow to bypass a denial.
If genuinely new read access is needed, put the exact proposed scoped grant on
the owning issue for the Director; agents never apply it.

From this skill's directory:

```sh
uv run scripts/launchplane-service-logs.py \
  --config /private/path/log-target.json \
  --since 2026-01-02T12:00:00Z --until 2026-01-02T12:10:00Z \
  --trace-id launchplane_req_example --lines 200
```

Both bounds require an explicit RFC3339 timezone. The start is inclusive and
the end exclusive, with nanosecond precision and a maximum 24-hour window.
`--lines` is positive and at most 1,000; the default is 200. `--timeout` bounds
each SSH read and each container's local parse (checked between size-bounded
records), defaults to 30 seconds and is at most 60. The helper reads at
most eight retained matching containers, newest first, reporting any older
omitted containers. Docker applies the time window at the source; the helper
independently checks timestamps and applies the final chronological line cap
after ID selection. No remote `--tail` is sent: some Docker drivers tail before
filtering the time window. Each read has a 2 MiB byte budget enforced while
draining both pipes. A byte or time limit fails explicitly with no partial
output; narrow the window before another read. Neither `--follow` nor
`--details` is used. The transport requires a POSIX workstation.

`--operation-id` and `--trace-id` are optional literal, case-sensitive matches
with identifier boundaries. Supplying both requires both on the same source
line. They do not expand or reconstruct a trace: use the bounded unfiltered
window for surrounding traceback/context lines. Selection reads the private
source text, so an ID subsequently hidden by redaction still matches. Output
contains timestamps, stream names, local container ordinals, redacted messages
and per-container lifecycle/image metadata, not private target selectors.
The `containers` array uses the same ordinal order as entries and includes
creation/start/finish times, state, exit code, OOM flag and `image_id`, the local
Docker image configuration ID (not a registry manifest digest). Old and replacement
instances can be distinguished without exposing host/container names. Docker merges
container stdout and stderr inside the remote log invocation, so entries use
`stream: "combined"`. SSH/LXC diagnostics are kept separate and never printed;
`transport_diagnostics_present` only reports that some were observed.

Redaction uses bounded JSON/Python-literal projection, sensitive field components,
name/key/variable paired with value/data/default/val in structured records, conservative plain
name/key pairs and split value fragments, and recognizable token, authorization, cookie and URL-credential
shapes. It normalizes at most three percent/JSON escape layers and bounds structured
nesting. Complete objects retain neighboring diagnostic fields such as trace IDs,
commit SHAs, image digests and code paths. A plain sensitive assignment discards
the rest of its record; an unparsed sensitive name/value pair masks the whole
record in either property order. Malformed structured records are masked.
A quote-aware balance scan decides completeness independently of literal parsing.
Complete structured or quoted values cannot open multiline continuation state;
nested serialized fields are projected without changing that state. Standalone
value/data/default fragments without an established ordinary name are masked,
including reversed or window-truncated name/value output.

Multiline PEM/PGP private keys are masked through their closing delimiter.
Sensitive YAML block headers (including scalar indicators, comments and anchors)
mask the block; after one such header, later indented records remain masked even
across interleaved unindented writers. This trades indented traceback detail for
safe continuation handling. Leading indented fragments are also masked because
their parent may precede the window. Later unindented ordinary records remain
readable. Flush-left multiline prose without a recognizable key delimiter cannot
be safely attributed to a writer; do not rely on redaction of that unsupported
framing.
Nested candidates inside an already rejected, balanced unquoted span do not spend
the literal-attempt budget. Exhausting that budget masks one record and preserves
its detected continuation; later ordinary records remain readable. Unknown quoted
representations, including object repr calls, are withheld as closed values.
An individual source message over 4 KiB, excessive encoding or an unmatched
private key end fails the whole read with a fixed code and no partial data. The size check
runs before parsing or pattern scans. Terminal controls are removed. Every record
is sanitized before output; matching IDs is private, literal and uses normalized
source text. `matched_record_count` and `changed_record_count` report selected
source records and those changed by sanitization or normalization (including
JSON reserialization), before the final line cap. They do not count secrets.
Arbitrary unlabelled prose cannot be classified as a secret reliably. Treat even
redacted output as private operational evidence, not publication-ready content;
publish only the necessary reviewed, sanitized conclusion.

For controller quota evidence, add `--tallies-only` to the same command. This
projection returns typed `requests`, `not_modified`, `non_304_requests` and
`seconds` fields, with repository, base branch, pass trace ID, scheduled/API
origin, timestamp and container ordinal. It accepts only complete, unaltered
INFO records from `control_plane.github_request_timing` in the current
`GitHub request tally: [scheduled ]merge-train controller ...` format, with
nonnegative counts whose two subsets sum to requests. Other formats are withheld;
there is no substring extraction or raw message field. These are observed
controller counts, not proof of charged quota, exclusive driver attribution or
an accepted overnight measurement.

The projection reuses the same framing, redaction, host/image selection, time,
byte and display limits. It keeps `complete: false` and
`coverage: retained_containers_only`, adds `projection: controller_tallies`, and
reports `withheld_record_count` for in-window records it could not project,
independently of ID selection. `matched_record_count` counts recognized,
ID-selected tallies before the display cap. Neither an empty result nor zero
withheld records proves no traffic or complete history.

An oversized message or another redaction error withholds that record and the
rest of that container's stream: its multiline state can no longer be trusted.
Earlier safe tallies and other containers remain available. Narrow the window
to a separately readable interval for later evidence; do not raise the 4 KiB
bound or infer counts from withheld bodies. Framing, transport, byte-budget and
timeout failures still return no partial result. The ordinary message mode
retains its original whole-read failure behavior.

Exit 0 emits `ok: true` JSON, including a possible empty `entries` list.
Exit 1 emits a fixed diagnostic code and no raw or partial log data. Codes
distinguish invalid bounds/config/identifiers, unavailable containers, an image
mismatch, invalid framing and a failed remote read; other local/transport
failures use `service_logs_unavailable`. `--help` prints usage and exits 0;
interruption emits a safe JSON error and exits 130. An SSH failure has no Launchplane
trace because no API request was made. The helper does not retry.

`complete` is always false and `coverage` is `retained_containers_only`.
`tail_limit_reached` reports an observed cap, not proof of complete coverage
when false. Container removal (including a race between listing and reading), rotation,
omitted containers, a failed read or
the display cap can leave the requested period unavailable. An empty window or ID
match is not proof that the operation never ran. Use the original operation's
supported readback for its disposition, without replaying it for diagnosis.

The helper writes nothing itself. Redirecting its JSON to private task evidence
is the caller's separate choice. Host audit logs may record the read, and the
existing SSH identity retains its existing permissions; this helper only
constructs container-list, bounded metadata-inspect and container-log commands.
