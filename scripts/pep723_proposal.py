#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence

from pep723_metadata import (
    PACKAGE_NAME_PATTERN,
    DependencyPolicyError,
    ScriptMetadata,
    normalize_package_name,
    parse_script,
    render_script,
)


# https://github.com/cbusillo/launchplane/blob/main/docs/service-boundary.md#updater-source-proposals
ROUTE_BODY_LIMIT = 1024 * 1024
ROUTE_CHANGE_LIMIT = 2 * 1024 * 1024
ROUTE_FILE_LIMIT = 32
ROUTE_PR_BODY_LIMIT = 16384
CANDIDATE_FILE = "candidate.json"
CANDIDATE_SCHEMA_VERSION = 1
WORKFLOW_PATH = ".github/workflows/update-pep723-dependencies.yml"
REPOSITORY_METADATA_PATH = ".github/github.json"
SUBMITTING_EVENTS = frozenset({"schedule", "workflow_dispatch"})
PROPOSAL_TITLE = "build(deps): update PEP 723 dependencies"
REGULAR_FILE_MODES = frozenset({"100644", "100755"})
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RUN_NUMBER = re.compile(r"^[1-9][0-9]*$")
UPDATER_BRANCH = re.compile(r"^launchplane/updater/[0-9a-f]{64}/[0-9a-f]{64}$")
# https://packaging.python.org/en/latest/specifications/version-specifiers/#normalization
CANONICAL_STABLE_VERSION = (
    r"(?:[1-9][0-9]*!)?(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*(?:\.post(?:0|[1-9][0-9]*))?"
)
EXACT_PIN = re.compile(
    rf"^(?P<name>{PACKAGE_NAME_PATTERN})==(?P<version>{CANONICAL_STABLE_VERSION})$"
)
DISPOSITION_RESULTS = {
    "created": "Created a new proposal. Ordinary CI, review and merge-train admission still apply.",
    "replayed": (
        "Replayed: this exact proposal already exists, open or closed. "
        "Nothing new was created or reopened."
    ),
    "pending": (
        "Pending: this run's candidate was not applied. The existing PEP 723 "
        "proposal needs ordinary review before another one can open."
    ),
}


class ProposalRefused(ValueError):
    pass


@dataclass(frozen=True)
class RunContext:
    repository: str
    base_sha: str
    run_id: str
    run_attempt: str

    def as_candidate_fields(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "base_sha": self.base_sha,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
        }


@dataclass(frozen=True)
class BaseSnapshot:
    modes: Mapping[str, str]
    sources: Mapping[str, str]


@dataclass(frozen=True)
class PreparedRequest:
    payload: bytes
    changed_files: int
    change_bytes: int


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProposalRefused(message)


def run_context(environment: Mapping[str, str]) -> RunContext:
    context = RunContext(
        repository=environment.get("GITHUB_REPOSITORY", ""),
        base_sha=environment.get("GITHUB_SHA", ""),
        run_id=environment.get("GITHUB_RUN_ID", ""),
        run_attempt=environment.get("GITHUB_RUN_ATTEMPT", ""),
    )
    require(
        REPOSITORY_NAME.fullmatch(context.repository) is not None
        and COMMIT_SHA.fullmatch(context.base_sha) is not None
        and RUN_NUMBER.fullmatch(context.run_id) is not None
        and RUN_NUMBER.fullmatch(context.run_attempt) is not None,
        "Invalid trusted run context.",
    )
    return context


def calculation_context(context: RunContext, environment: Mapping[str, str]) -> RunContext:
    calculated_in = environment.get("CANDIDATE_RUN_ATTEMPT", "")
    require(
        RUN_NUMBER.fullmatch(calculated_in) is not None
        and int(calculated_in) <= int(context.run_attempt),
        "The calculation job reported no attempt of this run.",
    )
    return RunContext(context.repository, context.base_sha, context.run_id, calculated_in)


def git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ProposalRefused(f"git {args[0]} failed: {detail}")
    return result.stdout


def decode_utf8(data: bytes, label: str) -> str:
    try:
        return data.decode()
    except UnicodeDecodeError as exc:
        raise ProposalRefused(f"{label} is not UTF-8 text.") from exc


def _read_blobs(cwd: Path, object_ids: Sequence[str]) -> list[bytes]:
    output = git(
        cwd, "cat-file", "--batch", stdin="".join(f"{item}\n" for item in object_ids).encode()
    )
    blobs: list[bytes] = []
    position = 0
    for object_id in object_ids:
        header_end = output.index(b"\n", position)
        header = output[position:header_end].decode("ascii").split(" ")
        require(
            len(header) == 3 and header[0] == object_id and header[1] == "blob",
            "Base object read did not return the expected blob.",
        )
        size = int(header[2])
        blobs.append(output[header_end + 1 : header_end + 1 + size])
        position = header_end + 1 + size + 1
    return blobs


def read_base_snapshot(base_sha: str, cwd: Path) -> BaseSnapshot:
    listing = git(cwd, "ls-tree", "-r", "-z", "--full-tree", base_sha)
    modes: dict[str, str] = {}
    object_ids: dict[str, str] = {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        metadata, _, raw_path = entry.partition(b"\t")
        mode, object_type, object_id = metadata.decode("ascii").split(" ")
        path = decode_utf8(raw_path, "A tracked path")
        if path.endswith(".py") and object_type == "blob" and mode in REGULAR_FILE_MODES:
            modes[path] = mode
            object_ids[path] = object_id
    paths = sorted(object_ids)
    blobs = _read_blobs(cwd, [object_ids[path] for path in paths])
    sources: dict[str, str] = {}
    for path, blob in zip(paths, blobs, strict=True):
        try:
            sources[path] = blob.decode()
        except UnicodeDecodeError:
            continue
    return BaseSnapshot(modes=modes, sources=sources)


def _pins(script: ScriptMetadata, label: str) -> dict[str, tuple[str, str]]:
    pins: dict[str, tuple[str, str]] = {}
    for value in script.dependencies:
        match = EXACT_PIN.fullmatch(value)
        require(match is not None, f"{label}: {value!r} is not an exact stable pin.")
        assert match is not None
        normalized = normalize_package_name(match.group("name"))
        require(normalized not in pins, f"{label}: {normalized!r} is listed twice.")
        pins[normalized] = (match.group("name"), match.group("version"))
    return pins


def _parse_metadata(path: str, source: str, label: str) -> ScriptMetadata | None:
    try:
        return parse_script(Path(path), source)
    except DependencyPolicyError as exc:
        raise ProposalRefused(f"{label}: {exc}") from exc


def _validated_file(path: str, base_source: str, content: str) -> tuple[ScriptMetadata, str]:
    base = _parse_metadata(path, base_source, f"base {path}")
    require(
        base is not None and base.has_dependencies_key,
        f"{path} has no PEP 723 dependencies to update.",
    )
    assert base is not None
    candidate = _parse_metadata(path, content, path)
    require(candidate is not None, f"{path} lost its PEP 723 metadata.")
    assert candidate is not None
    base_pins = _pins(base, f"base {path}")
    candidate_pins = _pins(candidate, path)
    require(
        set(candidate_pins) == set(base_pins),
        f"{path} adds or removes a dependency; only versions may change.",
    )
    require(
        all(candidate_pins[name][0] == base_pins[name][0] for name in base_pins),
        f"{path} renames a dependency; only versions may change.",
    )
    require(
        list(candidate_pins) == sorted(candidate_pins),
        f"{path} does not list dependencies in the updater's order.",
    )
    try:
        expected = render_script(base, candidate.dependencies)
    except DependencyPolicyError as exc:
        raise ProposalRefused(f"{path}: {exc}") from exc
    require(
        expected == content,
        f"{path} changes bytes outside the rendered PEP 723 dependency list.",
    )
    return candidate, expected


def _repository_versions(
    base: BaseSnapshot, changed: Mapping[str, ScriptMetadata]
) -> dict[str, tuple[str, str]]:
    versions: dict[str, tuple[str, str]] = {}
    for path, source in sorted(base.sources.items()):
        script = changed.get(path) or _parse_metadata(path, source, f"base {path}")
        if script is None:
            continue
        for normalized, (name, version) in _pins(script, path).items():
            prior = versions.setdefault(normalized, (name, version))
            require(
                prior[1] == version,
                f"{normalized} would be pinned to both {prior[1]} and {version}; "
                "the candidate must include every script that pins it.",
            )
    return versions


def _proposal_body(
    before: Mapping[str, tuple[str, str]], after: Mapping[str, tuple[str, str]], changed_files: int
) -> str:
    updates = [
        f"- `{after[name][0]}` {before[name][1]} → {after[name][1]}"
        for name in sorted(after)
        if before.get(name, after[name])[1] != after[name][1]
    ]
    body = "\n".join(
        [
            "## Summary",
            "",
            "Update repository-wide PEP 723 direct dependency pins from PyPI, keeping "
            "one exact version for each package.",
            "",
            *updates,
            "",
            f"Changed scripts: {changed_files}.",
            "",
            "## Validation",
            "",
            "The updater workflow ran `./scripts/validate-skills.sh` on this exact candidate "
            "in its unprivileged job. Its submit job then checked, without running candidate "
            "code, that only PEP 723 dependency pins changed.",
            "",
            "Ordinary CI, review and Launchplane merge-train admission still apply.",
        ]
    )
    require(len(body) <= ROUTE_PR_BODY_LIMIT, "The proposal description is too long.")
    return body + "\n"


def build_request(candidate: object, context: RunContext, base: BaseSnapshot) -> PreparedRequest:
    require(isinstance(candidate, dict), "The candidate must be a JSON object.")
    assert isinstance(candidate, dict)
    expected_fields = {"schema_version", "files", *context.as_candidate_fields()}
    require(
        set(candidate) == expected_fields
        and candidate["schema_version"] == CANDIDATE_SCHEMA_VERSION
        and all(candidate[key] == value for key, value in context.as_candidate_fields().items()),
        "The candidate does not belong to this exact run and base commit.",
    )
    files = candidate["files"]
    require(
        isinstance(files, dict) and 0 < len(files) <= ROUTE_FILE_LIMIT,
        f"The candidate must change between 1 and {ROUTE_FILE_LIMIT} files; "
        f"it changes {len(files) if isinstance(files, dict) else 'an invalid set of'} files.",
    )
    assert isinstance(files, dict)
    change_bytes = 0
    changed: dict[str, ScriptMetadata] = {}
    for path, content in sorted(files.items()):
        require(
            isinstance(path, str) and path in base.modes and path in base.sources,
            f"{path!r} is not an existing tracked regular UTF-8 Python file.",
        )
        require(isinstance(content, str), f"{path} content must be text.")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProposalRefused(f"{path} content is not valid UTF-8 text.") from exc
        base_source = base.sources[path]
        require(content != base_source, f"{path} is unchanged; omit it.")
        changed[path], _ = _validated_file(path, base_source, content)
        change_bytes += len(base_source.encode()) + len(encoded)
    require(
        change_bytes <= ROUTE_CHANGE_LIMIT,
        f"The change needs {change_bytes} old-plus-new bytes; the route allows {ROUTE_CHANGE_LIMIT}.",
    )
    before = _repository_versions(base, {})
    after = _repository_versions(base, changed)
    request = {
        "repository": context.repository,
        "base_sha": context.base_sha,
        "title": PROPOSAL_TITLE,
        "body": _proposal_body(before, after, len(changed)),
        "files": [{"path": path, "content": files[path]} for path in sorted(changed)],
    }
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    require(
        len(payload) <= ROUTE_BODY_LIMIT,
        f"The request is {len(payload)} bytes; the route allows {ROUTE_BODY_LIMIT}.",
    )
    return PreparedRequest(payload=payload, changed_files=len(changed), change_bytes=change_bytes)


def _require_checkout_at(base_sha: str, cwd: Path) -> None:
    head = git(cwd, "rev-parse", "HEAD").decode("ascii").strip()
    require(head == base_sha, "The checkout does not match the run commit.")


def _workspace_changes(base: BaseSnapshot, cwd: Path) -> dict[str, str]:
    status = git(cwd, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    files: dict[str, str] = {}
    for entry in status.split(b"\0"):
        if not entry:
            continue
        code, path = entry[:3], decode_utf8(entry[3:], "A changed path")
        require(
            code == b" M " and path in base.modes and path in base.sources,
            f"The updater may only modify tracked Python files; found {entry[:3].decode()!r} {path}.",
        )
        file_status = os.lstat(cwd / path)
        require(
            stat.S_ISREG(file_status.st_mode)
            and file_status.st_size <= ROUTE_BODY_LIMIT
            and bool(file_status.st_mode & 0o100) == (base.modes[path] == "100755"),
            f"{path} must stay a regular file with its tracked mode and a bounded size.",
        )
        content = decode_utf8((cwd / path).read_bytes(), path)
        if content != base.sources[path]:
            files[path] = content
    return files


def package_candidate(context: RunContext, output_directory: Path, cwd: Path) -> str | None:
    _require_checkout_at(context.base_sha, cwd)
    base = read_base_snapshot(context.base_sha, cwd)
    files = _workspace_changes(base, cwd)
    if not files:
        return None
    candidate = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        **context.as_candidate_fields(),
        "files": files,
    }
    build_request(candidate, context, base)
    data = json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode()
    output_directory.mkdir()
    with (output_directory / CANDIDATE_FILE).open("xb") as handle:
        handle.write(data)
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    require(len(set(keys)) == len(keys), "The candidate repeats a JSON key.")
    return dict(pairs)


def _reject_constant(value: str) -> object:
    raise ProposalRefused(f"The candidate contains the non-JSON constant {value}.")


def read_candidate(directory: Path, expected_digest: str) -> object:
    require(
        DIGEST.fullmatch(expected_digest) is not None,
        "The calculation job reported no candidate digest.",
    )
    directory_status = os.lstat(directory)
    require(
        stat.S_ISDIR(directory_status.st_mode) and os.listdir(directory) == [CANDIDATE_FILE],
        f"The artifact must contain only {CANDIDATE_FILE}.",
    )
    file_status = os.lstat(directory / CANDIDATE_FILE)
    require(
        stat.S_ISREG(file_status.st_mode) and file_status.st_size <= ROUTE_CHANGE_LIMIT,
        f"{CANDIDATE_FILE} must be a regular file of at most {ROUTE_CHANGE_LIMIT} bytes.",
    )
    data = (directory / CANDIDATE_FILE).read_bytes()
    require(
        hashlib.sha256(data).hexdigest() == expected_digest,
        "The artifact is not the candidate the calculation job packaged.",
    )
    try:
        return json.loads(
            decode_utf8(data, CANDIDATE_FILE),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ProposalRefused(f"{CANDIDATE_FILE} is not valid JSON.") from exc


def require_default_branch_workflow(
    environment: Mapping[str, str], base_sha: str, cwd: Path
) -> None:
    metadata = json.loads(git(cwd, "show", f"{base_sha}:{REPOSITORY_METADATA_PATH}"))
    default_branch = metadata.get("defaultBranch") if isinstance(metadata, dict) else None
    reference = environment.get("GITHUB_REF", "")
    require(
        isinstance(default_branch, str)
        and bool(default_branch)
        and environment.get("GITHUB_EVENT_NAME") in SUBMITTING_EVENTS
        and reference == f"refs/heads/{default_branch}"
        and environment.get("GITHUB_WORKFLOW_REF")
        == f"{environment.get('GITHUB_REPOSITORY')}/{WORKFLOW_PATH}@{reference}",
        "The updater submits only from its own scheduled or manual run on the default branch.",
    )


def prepare_request(
    context: RunContext, candidate_directory: Path, expected_digest: str, cwd: Path
) -> PreparedRequest:
    require(
        git(cwd, "status", "--porcelain=v1", "--untracked-files=all", "-z") == b"",
        "The trusted checkout is not clean.",
    )
    _require_checkout_at(context.base_sha, cwd)
    candidate = read_candidate(candidate_directory, expected_digest)
    return build_request(candidate, context, read_base_snapshot(context.base_sha, cwd))


def _failure_detail(response: object) -> str:
    details: list[str] = []
    if isinstance(response, dict):
        route_error = response.get("error")
        if isinstance(route_error, dict) and isinstance(route_error.get("code"), str):
            details.append(f"code `{route_error['code']}`")
        if isinstance(response.get("trace_id"), str):
            details.append(f"trace `{response['trace_id']}`")
    return ", ".join(details) or "no detail"


def _failure_guidance(status: str) -> str:
    if status == "403":
        return (
            "Launchplane denied this submission. Stop here: record the trace on the owning "
            "issue and do not publish another way."
        )
    if status == "409":
        return (
            "Launchplane reported a conflict. If main moved, the next run recalculates from "
            "the new main; otherwise record the trace on the owning issue."
        )
    return (
        "The effect is unconfirmed. Keep this run's request artifact for reconciliation: "
        "re-running this failed submit job resends the identical request, which returns any "
        "proposal it created. A new workflow run recalculates and may send a different request. "
        "No other publisher may be used."
    )


def summarize_response(
    response: object,
    repository: str,
    status: str,
    outcome: str,
    outputs: Mapping[str, str],
    request_digest: str,
) -> tuple[bool, str]:
    identity = f"Request sha256: `{request_digest}`\n"
    if outcome != "success" or re.fullmatch(r"2[0-9][0-9]", status) is None:
        label = f"HTTP {status}" if re.fullmatch(r"[0-9]{3}", status) else "no HTTP status"
        return False, (
            f"PEP 723 proposal failed ({label}; {_failure_detail(response)}). "
            f"{_failure_guidance(status)}\n\n{identity}"
        )
    number = response.get("pull_request_number") if isinstance(response, dict) else None
    confirmed = (
        isinstance(response, dict)
        and response.get("status") == "ok"
        and response.get("repository") == repository
        and response.get("disposition") in DISPOSITION_RESULTS
        and isinstance(response.get("head_sha"), str)
        and COMMIT_SHA.fullmatch(response["head_sha"]) is not None
        and isinstance(number, int)
        and not isinstance(number, bool)
        and number > 0
        and response.get("pull_request_url") == f"https://github.com/{repository}/pull/{number}"
        and isinstance(response.get("branch"), str)
        and UPDATER_BRANCH.fullmatch(response["branch"]) is not None
        and all(outputs.get(field) == response.get(field) for field in outputs)
    )
    if not confirmed:
        return False, (
            "Malformed Launchplane response; the proposal's effect is unconfirmed. "
            f"{_failure_guidance('')}\n\n{identity}"
        )
    assert isinstance(response, dict)
    return True, (
        f"{DISPOSITION_RESULTS[response['disposition']]}\n\n{response['pull_request_url']}\n\n"
        f"Returned head: `{response['head_sha']}`\n\n{identity}"
    )


def _read_response(path: Path) -> object:
    try:
        file_status = os.lstat(path)
        if not stat.S_ISREG(file_status.st_mode) or file_status.st_size > ROUTE_BODY_LIMIT:
            return None
        return json.loads(path.read_bytes().decode())
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _append(path: str, text: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Package, validate and report a PEP 723 updater proposal as data."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package", help="Package the updater's workspace change")
    package.add_argument("output_directory", type=Path)
    prepare = commands.add_parser("prepare", help="Validate the artifact and write the request")
    prepare.add_argument("candidate_directory", type=Path)
    prepare.add_argument("request_file", type=Path)
    summary = commands.add_parser("summary", help="Report the route's answer")
    summary.add_argument("request_file", type=Path)
    summary.add_argument("response_file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None, environment: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    environment = os.environ if environment is None else environment
    context = run_context(environment)
    cwd = Path.cwd()
    if args.command == "package":
        digest = package_candidate(context, args.output_directory, cwd)
        _append(
            environment["GITHUB_OUTPUT"],
            f"changed={'true' if digest else 'false'}\ndigest={digest or ''}\n",
        )
        print(f"candidate sha256: {digest}" if digest else "no PEP 723 dependency change")
        return 0
    if args.command == "prepare":
        require_default_branch_workflow(environment, context.base_sha, cwd)
        prepared = prepare_request(
            calculation_context(context, environment),
            args.candidate_directory,
            environment.get("EXPECTED_DIGEST", ""),
            cwd,
        )
        with args.request_file.open("xb") as handle:
            handle.write(prepared.payload)
        print(
            f"request: {prepared.changed_files} file(s), {len(prepared.payload)} of "
            f"{ROUTE_BODY_LIMIT} body bytes, {prepared.change_bytes} of {ROUTE_CHANGE_LIMIT} "
            f"old-plus-new bytes, sha256 {hashlib.sha256(prepared.payload).hexdigest()}"
        )
        return 0
    outputs = {
        field: environment.get(name, "")
        for field, name in (
            ("disposition", "RESPONSE_DISPOSITION"),
            ("pull_request_url", "RESPONSE_PULL_REQUEST_URL"),
            ("head_sha", "RESPONSE_HEAD_SHA"),
        )
    }
    confirmed, report = summarize_response(
        _read_response(args.response_file),
        context.repository,
        environment.get("SUBMIT_STATUS", ""),
        environment.get("SUBMIT_OUTCOME", ""),
        outputs,
        hashlib.sha256(args.request_file.read_bytes()).hexdigest(),
    )
    print(report)
    _append(environment["GITHUB_STEP_SUMMARY"], report)
    return 0 if confirmed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProposalRefused as error:
        print(f"PEP 723 proposal stopped: {error}", file=sys.stderr)
        raise SystemExit(1) from error
