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
requires every selected image to match the configured repository. It refuses
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
each SSH read, defaults to 30 seconds and is at most 60. The helper reads at
most eight retained matching containers. Docker applies the window and tail
at the source; the helper independently checks timestamps and caps the final
chronological output. Neither `--follow` nor `--details` is used.

`--operation-id` and `--trace-id` are optional literal, case-sensitive matches
with identifier boundaries. Supplying both requires both on the same redacted
line. They do not expand or reconstruct a trace: use the bounded unfiltered
window for surrounding traceback/context lines. Identifiers hidden by redaction
cannot match. Output contains only timestamps, stream names, local container
ordinals, redacted messages and coverage metadata, not private target selectors.

Redaction covers secret assignments (including nested JSON and unquoted
values), authorization and cookie headers, URL credentials, recognizable
tokens, opaque key-shaped text and multiline PEM private keys. A sensitive
assignment discards the rest of its line so nested or malformed values cannot
leak fragments. Terminal controls are removed. Redaction happens before ID
filtering and output; errors never echo raw SSH/provider text or credentials.
Arbitrary unlabelled prose cannot be classified as a secret reliably. Treat
even redacted output as private operational evidence, not publication-ready
content; publish only the necessary reviewed, sanitized conclusion.

Exit 0 emits `ok: true` JSON, including a possible empty `entries` list.
Exit 1 emits a fixed diagnostic code and no raw or partial log data. Codes
distinguish invalid bounds/config/identifiers, unavailable containers, an image
mismatch, invalid framing and a failed remote read; other local/transport
failures use `service_logs_unavailable`. An SSH failure has no Launchplane
trace because no API request was made. The helper does not retry.

`complete` is always false and `coverage` is `retained_container_tails_only`.
`tail_limit_reached` reports an observed cap, not proof of complete coverage
when false. Container removal, rotation, a replaced instance, a failed read or
a tail cap can leave the requested period unavailable. An empty window or ID
match is not proof that the operation never ran. Use the original operation's
supported readback for its disposition, without replaying it for diagnosis.

The helper writes nothing itself. Redirecting its JSON to private task evidence
is the caller's separate choice. Host audit logs may record the read, and the
existing SSH identity retains its existing permissions; this helper only
constructs container-list and container-log commands.
