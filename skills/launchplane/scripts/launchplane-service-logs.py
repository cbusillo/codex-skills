#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read a bounded, redacted window of Launchplane's own retained container logs."""

import argparse
import json
import os
import re
import selectors
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

MAX_WINDOW_NS = 24 * 60 * 60 * 10**9
MAX_LINES = 1000
MAX_CONTAINERS = 8
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SELECTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z")
TIMESTAMP = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)\Z"
)
CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")
SECRET_NAME = r"[A-Za-z0-9_.-]*(?:password|passwd|pwd|token|secret|credential|cookie|authorization|key)[A-Za-z0-9_.-]*"
ASSIGNMENT = re.compile(rf"""(?i)["']?\b{SECRET_NAME}["']?\s*[=:]\s*""")
HEADER = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*.*"
)
BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;\"']+")
URL_CREDENTIAL = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+@")
TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk[-_][A-Za-z0-9_-]+|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
OPAQUE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{64,}(?![A-Za-z0-9+/=_-])")
PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
PEM_END = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")


class LogReadError(ValueError):
    """An internal, fixed diagnostic code; never constructed from remote text."""


def timestamp(value: str) -> int:
    if not TIMESTAMP.fullmatch(value):
        raise LogReadError("invalid_timestamp")
    parsed = datetime.fromisoformat(value).astimezone(UTC)
    fractional = re.search(r"\.(\d+)", value)
    nanos = int(fractional[1].ljust(9, "0")) if fractional else 0
    return int(parsed.replace(microsecond=0).timestamp()) * 10**9 + nanos


def load_target(path: str) -> dict:
    # These are explicit private selectors, not credentials or runtime defaults.
    target = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
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


def read_remote(target: dict, docker: list[str], timeout: float) -> tuple[bytes, bytes]:
    command = (
        ["pct", "exec", str(target["lxc_id"]), "--", *docker]
        if "lxc_id" in target
        else docker
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
                "--",
                target["ssh_host"],
                shlex.join(command),
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
            if process.wait(timeout=max(0, deadline - time.monotonic())):
                # Never return an SSH/provider error body or partial raw output.
                raise LogReadError("remote_read_failed")
        except BaseException:
            process.kill()
            process.wait()
            raise
        return b"".join(buffers[0]), b"".join(buffers[1])


def redact(message: str) -> str:
    message = CONTROL.sub("", message)
    # Unframed final PEM fragments can be all that survives --tail/rotation.
    if re.fullmatch(r"[A-Za-z0-9+/=]{16,}", message.strip()):
        return "[redacted]"
    message = HEADER.sub("[redacted-header]", message)
    message = BEARER.sub("[redacted-authorization]", message)
    message = URL_CREDENTIAL.sub(r"\1[redacted]@", message)
    message = TOKEN.sub("[redacted]", message)
    message = OPAQUE.sub("[redacted]", message)
    sensitive = ASSIGNMENT.search(message)
    # Discard the remainder, including nested JSON, unquoted spaces and trailing
    # cookie attributes. Trying to guess the value's end can expose fragments.
    return message[: sensitive.end()] + "[redacted]" if sensitive else message


def entries_from_stream(
    raw: bytes, stream: str, container: int, since: int, until: int
) -> list[dict]:
    entries = []
    private_key = False
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line:
            continue
        stamp, separator, message = line.partition(" ")
        if not separator:
            raise LogReadError("invalid_log_framing")
        observed = timestamp(stamp)
        if PEM_BEGIN.search(message):
            private_key = True
        if PEM_END.search(message) and not private_key:
            # --since/--tail can omit BEGIN but retain short final key fragments.
            # Without the start delimiter, redact the preceding stream as well.
            for entry in entries:
                entry["message"] = "[redacted-private-key]"
            private_key = True
        safe_message = "[redacted-private-key]" if private_key else redact(message)
        if PEM_END.search(message):
            private_key = False
        if since <= observed < until:
            entries.append(
                {
                    "timestamp": stamp,
                    "stream": stream,
                    "container": container,
                    "message": safe_message,
                }
            )
    return entries


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
    output, _ = read_remote(
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
            "{{.ID}} {{.Image}}",
        ],
        args.timeout,
    )
    candidates = output.decode("utf-8").splitlines()
    if not 1 <= len(candidates) <= MAX_CONTAINERS:
        raise LogReadError("service_container_unavailable")
    containers = []
    for candidate in candidates:
        fields = candidate.split()
        if len(fields) != 2 or not re.fullmatch(r"[a-f0-9]{64}", fields[0]):
            raise LogReadError("invalid_container_identity")
        image = fields[1].split("@", 1)[0]
        image = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
        if image != target["image_repository"]:
            raise LogReadError("unexpected_service_image")
        containers.append(fields[0])
    entries = []
    tail_limit_reached = False
    for index, container in enumerate(containers):
        stdout, stderr = read_remote(
            target,
            [
                "docker",
                "logs",
                "--timestamps",
                "--since",
                args.since,
                "--until",
                args.until,
                "--tail",
                str(args.lines),
                container,
            ],
            args.timeout,
        )
        tail_limit_reached |= (
            len(stdout.splitlines()) + len(stderr.splitlines()) >= args.lines
        )
        for stream, raw in (("stdout", stdout), ("stderr", stderr)):
            entries.extend(entries_from_stream(raw, stream, index, since, until))
    # Filter only redacted text. Selectors are literals with identifier boundaries.
    for value in (args.operation_id, args.trace_id):
        if value:
            pattern = re.compile(
                r"(?<![A-Za-z0-9_.:/-])" + re.escape(value) + r"(?![A-Za-z0-9_.:/-])"
            )
            entries = [entry for entry in entries if pattern.search(entry["message"])]
    entries.sort(key=lambda entry: timestamp(entry["timestamp"]))
    tail_limit_reached |= len(entries) > args.lines
    return {
        "ok": True,
        "since": args.since,
        "until": args.until,
        "entries": entries[-args.lines :],
        "redacted": True,
        "container_count": len(containers),
        "tail_limit_reached": tail_limit_reached,
        # A retained-container tail cannot prove historical coverage or absence.
        "complete": False,
        "coverage": "retained_container_tails_only",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--since", required=True, help="RFC3339 start, inclusive")
    parser.add_argument(
        "--until",
        required=True,
        help="RFC3339 end, exclusive; at most 24 hours after start",
    )
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--lines", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        result = read_logs(args)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        code = (
            str(error)
            if isinstance(error, LogReadError)
            else "service_logs_unavailable"
        )
        print(json.dumps({"ok": False, "error": code, "complete": False}))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
