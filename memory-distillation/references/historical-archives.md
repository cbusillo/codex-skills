# Historical archive distillation

Read only when the user explicitly asks to distill Chronicle or other private
screen-history archives. These are historical evidence, never a live context
provider or a requirement to restore a retired client.

Archives can contain screenshots, OCR, messages, window titles, paths, URLs,
credentials, job IDs, dashboards, and database details. Keep processing local
unless the user approves the exact remote destination and scope.

1. Establish the authorized archive path, provenance, and covered dates. Evaluate
   persisted contents only; preserve the originals.
2. If a local LLM would help, use it as a read-only scout to summarize, cluster,
   and point to evidence. The primary agent reads the scout output and owns
   classification, verification, proposals, and approved writes. Use the optional
   `local-llm` skill to establish endpoint locality/trust and perform a harmless
   readiness check before sending private source material. If the endpoint is
   cloud, unknown, disabled, or untrusted, do not send raw input without exact
   destination and scope approval.
3. Sample first. Use chunked passes only when the sample supports broader work
   within scope. Keep scout notes private; expect drafts to leak identifiers.
4. Produce a separate sanitized report. Drop credentials, private messages,
   hostnames, customer data, machine values, local paths, job/database details,
   screenshot/OCR paths, and account details before public promotion or sharing.
5. Verify candidates against maintained sources. Do not promote transient PR,
   job, deployment, or CI status as permanent memory. Route durable procedures
   to skills, repo-specific knowledge to maintained docs/issues, and private
   values or person facts to approved local config.
6. For an authorized recurring scan, use an approved private state location
   belonging to the current client for a metadata-only cursor: schema version,
   archive identity, last checked time, last file/mtime, processed count, and
   scout method. Do not store extracted facts or raw archive content in it.
   Use that cursor to avoid rescanning unchanged files unless requested.
7. Propose only concise, verified conclusions. Reuse scoped approval when
   present; obtain missing authority before writing destinations or the cursor.
   Never ingest an archive wholesale into reusable memory.
