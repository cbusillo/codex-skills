#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read a bounded, redacted window of Launchplane's own retained container logs."""

import argparse
import heapq
import json
import os
import re
import selectors
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launchplane_service_log_redaction import (
    RedactionError,
    Redactor,
)

MAX_WINDOW_NS = 24 * 60 * 60 * 10**9
MAX_LINES = 1000
MAX_CONTAINERS = 8
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SELECTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z")
TIMESTAMP = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)\Z"
)
TALLY = re.compile(
    r"INFO:control_plane[.]github_request_timing:GitHub request tally: "
    r"(?P<scheduled>scheduled )?merge-train controller "
    r"(?P<repository>[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})@"
    r"(?P<base_branch>[A-Za-z0-9_./-]{1,200}) trace "
    r"(?P<trace_id>[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}); "
    r"requests=(?P<requests>[0-9]{1,12}) "
    r"not_modified=(?P<not_modified>[0-9]{1,12}) "
    r"non_304_requests=(?P<non_304_requests>[0-9]{1,12}) "
    r"seconds=(?P<seconds>[0-9]{1,12}[.][0-9])"
)
INSPECT_FORMAT = '{"id":{{json .Id}},"image":{{json .Config.Image}},"image_id":{{json .Image}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"created_at":{{json .Created}},"started_at":{{json .State.StartedAt}},"finished_at":{{json .State.FinishedAt}},"state":{{json .State.Status}},"exit_code":{{json .State.ExitCode}},"oom_killed":{{json .State.OOMKilled}}}'


class LogReadError(ValueError):
    """An internal, fixed diagnostic code; never constructed from remote text."""


def timestamp(value: str) -> int:
    if not TIMESTAMP.fullmatch(value):
        raise LogReadError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError):
        raise LogReadError("invalid_timestamp") from None
    fractional = re.search(r"\.(\d+)", value)
    nanos = int(fractional[1].ljust(9, "0")) if fractional else 0
    return int(parsed.replace(microsecond=0).timestamp()) * 10**9 + nanos


def load_target(path: str) -> dict:
    # These are explicit private selectors, not credentials or runtime defaults.
    try:
        target = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise LogReadError("invalid_config") from None
    required = {"ssh_host", "compose_project", "compose_service", "image_repository"}
    if (
        not isinstance(target, dict)
        or not required <= target.keys()
        or target.keys() - required - {"lxc_id"}
    ):
        raise LogReadError("invalid_config")
    if any(
        not isinstance(target[key], str) or not SELECTOR.fullmatch(target[key])
        for key in ("compose_project", "compose_service")
    ):
        raise LogReadError("invalid_config")
    if (
        not isinstance(target["ssh_host"], str)
        or not all(SELECTOR.fullmatch(part) for part in target["ssh_host"].split("@"))
        or target["ssh_host"].count("@") > 1
    ):
        raise LogReadError("invalid_config")
    repository = target["image_repository"]
    if (
        not isinstance(repository, str)
        or not re.fullmatch(
            r"[a-z0-9][a-z0-9.:/-]{0,254}/[a-z0-9][a-z0-9_./-]{0,254}", repository
        )
        or ".." in repository
    ):
        raise LogReadError("invalid_config")
    if "lxc_id" in target and (
        type(target["lxc_id"]) is not int or target["lxc_id"] < 1
    ):
        raise LogReadError("invalid_config")
    return target


def read_remote(target: dict, docker: list[str], timeout: float) -> tuple[bytes, bool]:
    # Redirect inside the target's shell, so pct/SSH stderr stays transport-only.
    target_command = (
        ["sh", "-c", shlex.join(docker) + " 2>&1"]
        if docker[:2] == ["docker", "logs"]
        else docker
    )
    command = (
        ["pct", "exec", str(target["lxc_id"]), "--", *target_command]
        if "lxc_id" in target
        else target_command
    )
    with (
        subprocess.Popen(
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UpdateHostKeys=no",
                "-o",
                "ControlPath=none",
                "-o",
                "ClearAllForwardings=yes",
                "-o",
                "PermitLocalCommand=no",
                "--",
                target["ssh_host"],
                shlex.join(["env", "LC_ALL=C", *command]),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process,
        selectors.DefaultSelector() as selector,
    ):
        assert process.stdout is not None and process.stderr is not None
        buffers: list[list[bytes]] = [[], []]
        selector.register(process.stdout, selectors.EVENT_READ, 0)
        selector.register(process.stderr, selectors.EVENT_READ, 1)
        deadline = time.monotonic() + timeout
        size = 0
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LogReadError("remote_read_timeout")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, min(65536, MAX_RESPONSE_BYTES + 1 - size))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise LogReadError("remote_response_too_large")
                    buffers[key.data].append(chunk)
            try:
                return_code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise LogReadError("remote_read_timeout") from None
            if return_code:
                # Never return an SSH/provider error body or partial raw output.
                raise LogReadError("remote_read_failed")
        except BaseException:
            process.kill()
            process.wait()
            raise
        return b"".join(buffers[0]), bool(buffers[1])


def project_tally(message: str) -> dict | None:
    match = TALLY.fullmatch(message)
    if match is None:
        return None
    counts = {name: int(match[name]) for name in ("requests", "not_modified", "non_304_requests")}
    if counts["requests"] != counts["not_modified"] + counts["non_304_requests"]:
        return None
    return {
        **counts,
        "seconds": float(match["seconds"]),
        "repository": match["repository"],
        "base_branch": match["base_branch"],
        "trace_id": match["trace_id"],
        "scheduled": match["scheduled"] is not None,
    }


def entries_from_stream(
    raw: bytes,
    container: int,
    since: int,
    until: int,
    identifiers: tuple[str, ...],
    limit: int,
    timeout: float,
    tallies_only: bool = False,
) -> tuple[list[dict], int, int, int]:
    redactor = Redactor(identifiers)
    kept: list[tuple[int, int, dict]] = []
    matched = changed = withheld = 0
    quarantined = False
    deadline = time.monotonic() + timeout
    for sequence, line in enumerate(raw.decode("utf-8", errors="replace").split("\n")):
        if time.monotonic() >= deadline:
            raise LogReadError("local_parse_timeout")
        if not line:
            continue
        stamp, separator, message = line.partition(" ")
        if not separator:
            raise LogReadError("invalid_log_framing")
        try:
            observed = timestamp(stamp)
        except ValueError:
            raise LogReadError("invalid_log_framing") from None
        in_window = since <= observed < until
        if tallies_only and quarantined:
            withheld += in_window
            continue
        try:
            safe = redactor.feed(message)
        except RedactionError:
            if not tallies_only:
                raise
            quarantined = True
            withheld += in_window
            continue
        tally = project_tally(message) if tallies_only and not safe.redacted else None
        if time.monotonic() >= deadline:
            raise LogReadError("local_parse_timeout")
        if tallies_only and tally is None:
            withheld += in_window
            continue
        if in_window and safe.matched:
            matched += 1
            changed += safe.redacted
            entry = {
                "timestamp": stamp,
                "stream": "combined",
                "container": container,
                **(tally if tallies_only else {"message": safe.message}),
            }
            heapq.heappush(kept, (observed, sequence, entry))
            if len(kept) > limit:
                heapq.heappop(kept)
    return [item[2] for item in sorted(kept)], matched, changed, withheld


def inspect_containers(
    target: dict, identities: list[str], timeout: float
) -> tuple[list[dict], bool]:
    output, transport_diagnostics = read_remote(
        target,
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            INSPECT_FORMAT,
            *identities,
        ],
        timeout,
    )
    records = [json.loads(line) for line in output.decode("utf-8").split("\n") if line]
    if len(records) != len(identities):
        raise LogReadError("invalid_container_identity")
    for record, identity in zip(records, identities, strict=True):
        if (
            not isinstance(record, dict)
            or record.get("id") != identity
            or record.get("project") != target["compose_project"]
            or record.get("service") != target["compose_service"]
        ):
            raise LogReadError("invalid_container_identity")
        image = record.get("image")
        if not isinstance(image, str):
            raise LogReadError("invalid_container_identity")
        image = image.split("@", 1)[0]
        image = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
        if image != target["image_repository"]:
            raise LogReadError("unexpected_service_image")
        for field in ("created_at", "started_at", "finished_at"):
            if not isinstance(record.get(field), str):
                raise LogReadError("invalid_container_identity")
            timestamp(record[field])
        if (
            record.get("state")
            not in {
                "created",
                "restarting",
                "running",
                "removing",
                "paused",
                "exited",
                "dead",
            }
            or type(record.get("exit_code")) is not int
            or type(record.get("oom_killed")) is not bool
            or not isinstance(record.get("image_id"), str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", record["image_id"])
        ):
            raise LogReadError("invalid_container_identity")
    return [
        {
            field: record[field]
            for field in (
                "created_at",
                "started_at",
                "finished_at",
                "state",
                "exit_code",
                "oom_killed",
                "image_id",
            )
        }
        for record in records
    ], transport_diagnostics


def read_logs(args: argparse.Namespace) -> dict:
    since, until = timestamp(args.since), timestamp(args.until)
    if (
        not 0 < until - since <= MAX_WINDOW_NS
        or not 1 <= args.lines <= MAX_LINES
        or not 0 < args.timeout <= 60
    ):
        raise LogReadError("invalid_bounds")
    for value in (args.operation_id, args.trace_id):
        if value and not IDENTIFIER.fullmatch(value):
            raise LogReadError("invalid_identifier")
    target = load_target(args.config)
    output, transport_diagnostics = read_remote(
        target,
        [
            "docker",
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={target['compose_project']}",
            "--filter",
            f"label=com.docker.compose.service={target['compose_service']}",
            "--format",
            "{{.ID}}",
        ],
        args.timeout,
    )
    candidates = output.decode("utf-8").splitlines()
    if not candidates:
        raise LogReadError("service_container_unavailable")
    for candidate in candidates:
        if not re.fullmatch(r"[a-f0-9]{64}", candidate):
            raise LogReadError("invalid_container_identity")
    containers = candidates[:MAX_CONTAINERS]
    coverage, inspect_diagnostics = inspect_containers(target, containers, args.timeout)
    transport_diagnostics |= inspect_diagnostics
    entries = []
    matched = changed = withheld = 0
    identifiers = (args.operation_id, args.trace_id)
    for index, container in enumerate(containers):
        stdout, log_diagnostics = read_remote(
            target,
            [
                "docker",
                "logs",
                "--timestamps",
                "--since",
                args.since,
                "--until",
                args.until,
                container,
            ],
            args.timeout,
        )
        transport_diagnostics |= log_diagnostics
        kept, count, altered, hidden = entries_from_stream(
            stdout, index, since, until, identifiers, args.lines, args.timeout, args.tallies_only
        )
        entries.extend(kept)
        matched += count
        changed += altered
        withheld += hidden
    entries.sort(key=lambda entry: timestamp(entry["timestamp"]))
    return {
        "ok": True,
        **({"projection": "controller_tallies", "withheld_record_count": withheld} if args.tallies_only else {}),
        "since": args.since,
        "until": args.until,
        "entries": entries[-args.lines :],
        "redacted": True,
        "container_count": len(containers),
        "containers": coverage,
        "omitted_containers": len(candidates) - len(containers),
        "tail_limit_reached": matched > args.lines,
        "matched_record_count": matched,
        "changed_record_count": changed,
        "transport_diagnostics_present": transport_diagnostics,
        # A retained-container tail cannot prove historical coverage or absence.
        "complete": False,
        "coverage": "retained_containers_only",
    }


def main(argv: list[str] | None = None) -> int:
    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            raise LogReadError("invalid_arguments")

    parser = Parser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--since", required=True, help="RFC3339 start, inclusive")
    parser.add_argument(
        "--until",
        required=True,
        help="RFC3339 end, exclusive; at most 24 hours after start",
    )
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--tallies-only", action="store_true", help="Project recognized controller counts; withhold all log bodies")
    parser.add_argument("--lines", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=30)
    try:
        args = parser.parse_args(argv)
        result = read_logs(args)
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "interrupted", "complete": False}))
        return 130
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        code = (
            str(error)
            if isinstance(error, (LogReadError, RedactionError))
            else "service_logs_unavailable"
        )
        print(json.dumps({"ok": False, "error": code, "complete": False}))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
