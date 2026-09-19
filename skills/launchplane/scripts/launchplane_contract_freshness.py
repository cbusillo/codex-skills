"""Compare and report Launchplane agent/operator contract freshness."""
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import http.client
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from launchplane_contract import (
    ContractError,
    load_contract,
    validate_semantic_artifact,
)
from launchplane_safety import LaunchplaneSafetyError, safe_urlopen

EVIDENCE_SCHEMA_VERSION = "1.0"
DEFAULT_UPSTREAM_REPOSITORY = "cbusillo/launchplane"
DEFAULT_UPSTREAM_REF = "main"
DEFAULT_UPSTREAM_PATH = "contracts/agent-operator-contract.json"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 1.0
MAX_ARTIFACT_BYTES = 256 * 1024
MAINTENANCE_ISSUE_TITLE = "Refresh vendored Launchplane agent/operator contract"
MAINTENANCE_ISSUE_KEY = "launchplane-agent-operator-contract-freshness"
MAINTENANCE_ISSUE_MARKER = f"<!-- {MAINTENANCE_ISSUE_KEY} -->"

REPO_ROOT = Path(__file__).resolve().parents[2]
GH_PLAN_HELPER = REPO_ROOT / "github" / "scripts" / "gh-plan.py"
GH_ISSUE_HELPER = REPO_ROOT / "github" / "scripts" / "github_issue.py"

_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REF_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA_PATTERN = re.compile(r"^(unknown|[0-9a-f]{40}|[0-9a-f]{64})$")


class FreshnessError(ValueError):
    """Public-safe freshness failure."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


CommandRunner = Callable[[list[str], str | None], dict[str, Any]]


def _validate_source(repository: str, ref: str, path: str) -> None:
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise FreshnessError("invalid_upstream_repository")
    if not _REF_PATTERN.fullmatch(ref) or ref in {".", ".."}:
        raise FreshnessError("invalid_upstream_ref")
    if not _PATH_PATTERN.fullmatch(path) or ".." in path.split("/"):
        raise FreshnessError("invalid_upstream_path")
    if (repository, ref, path) != (
        DEFAULT_UPSTREAM_REPOSITORY,
        DEFAULT_UPSTREAM_REF,
        DEFAULT_UPSTREAM_PATH,
    ):
        raise FreshnessError("unapproved_upstream_source")


def upstream_url(repository: str, ref: str, path: str) -> str:
    _validate_source(repository, ref, path)
    encoded_repository = "/".join(
        urllib.parse.quote(part, safe="") for part in repository.split("/")
    )
    encoded_ref = urllib.parse.quote(ref, safe="")
    encoded_path = "/".join(
        urllib.parse.quote(part, safe="") for part in path.split("/")
    )
    return (
        "https://raw.githubusercontent.com/"
        f"{encoded_repository}/{encoded_ref}/{encoded_path}"
    )


def fetch_upstream_artifact(
    *,
    repository: str,
    ref: str,
    path: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY_SECONDS,
) -> bytes:
    if timeout <= 0:
        raise FreshnessError("invalid_timeout")
    if attempts <= 0:
        raise FreshnessError("invalid_attempts")
    if retry_delay < 0:
        raise FreshnessError("invalid_retry_delay")
    request = urllib.request.Request(
        upstream_url(repository, ref, path),
        headers={
            "Accept": "application/json",
            "User-Agent": "codex-skills-launchplane-contract-freshness/1",
        },
    )
    for attempt in range(attempts):
        try:
            with safe_urlopen(request, timeout=timeout) as response:
                body = response.read(MAX_ARTIFACT_BYTES + 1)
            if len(body) > MAX_ARTIFACT_BYTES:
                raise FreshnessError("upstream_response_too_large")
            return body
        except FreshnessError:
            raise
        except (
            LaunchplaneSafetyError,
            http.client.HTTPException,
            OSError,
            TimeoutError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as exc:
            if attempt + 1 >= attempts:
                raise FreshnessError("upstream_unavailable", retryable=True) from exc
            time.sleep(retry_delay)
    raise FreshnessError("upstream_unavailable", retryable=True)


def _artifact_identity(artifact: Mapping[str, Any]) -> dict[str, object]:
    provenance = artifact["provenance"]
    return {
        "schema_version": artifact["schema_version"],
        "normalization_version": artifact["normalization_version"],
        "semantic_digest_sha256": artifact["semantic_digest_sha256"],
        "source_commit_sha": provenance["source_commit_sha"],
    }


def _evidence(
    *,
    classification: str,
    retryable: bool,
    repository: str,
    ref: str,
    path: str,
    vendored: Mapping[str, Any] | None,
    upstream: Mapping[str, Any] | None,
    error_code: str | None = None,
    detail_code: str | None = None,
) -> dict[str, Any]:
    digest_match = None
    if vendored is not None and upstream is not None:
        digest_match = (
            vendored["semantic_digest_sha256"] == upstream["semantic_digest_sha256"]
        )
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "classification": classification,
        "retryable": retryable,
        "source": {
            "repository": repository,
            "ref": ref,
            "path": path,
        },
        "vendored": dict(vendored) if vendored is not None else None,
        "upstream": dict(upstream) if upstream is not None else None,
        "comparison": {"semantic_digest_match": digest_match},
        "error": (
            {"code": error_code, "detail_code": detail_code}
            if error_code is not None
            else None
        ),
        "summary": {
            "advisory_only": True,
            "runtime_authority": False,
            "local_conformance_independent": True,
        },
    }


def compare_artifacts(
    vendored_artifact: Mapping[str, Any],
    upstream_artifact: Mapping[str, Any],
    *,
    repository: str = DEFAULT_UPSTREAM_REPOSITORY,
    ref: str = DEFAULT_UPSTREAM_REF,
    path: str = DEFAULT_UPSTREAM_PATH,
) -> dict[str, Any]:
    _validate_source(repository, ref, path)
    validate_semantic_artifact(vendored_artifact)
    validate_semantic_artifact(upstream_artifact)
    vendored = _artifact_identity(vendored_artifact)
    upstream = _artifact_identity(upstream_artifact)
    classification = (
        "current"
        if vendored["semantic_digest_sha256"] == upstream["semantic_digest_sha256"]
        else "known-stale"
    )
    return _evidence(
        classification=classification,
        retryable=False,
        repository=repository,
        ref=ref,
        path=path,
        vendored=vendored,
        upstream=upstream,
    )


def evaluate_freshness(
    *,
    repository: str = DEFAULT_UPSTREAM_REPOSITORY,
    ref: str = DEFAULT_UPSTREAM_REF,
    path: str = DEFAULT_UPSTREAM_PATH,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY_SECONDS,
    fetcher: Callable[..., bytes] = fetch_upstream_artifact,
) -> dict[str, Any]:
    try:
        _validate_source(repository, ref, path)
    except FreshnessError as exc:
        return _evidence(
            classification="unknown",
            retryable=False,
            repository=DEFAULT_UPSTREAM_REPOSITORY,
            ref=DEFAULT_UPSTREAM_REF,
            path=DEFAULT_UPSTREAM_PATH,
            vendored=None,
            upstream=None,
            error_code=exc.code,
        )
    try:
        vendored_artifact = load_contract()
    except ContractError as exc:
        return _evidence(
            classification="unknown",
            retryable=False,
            repository=repository,
            ref=ref,
            path=path,
            vendored=None,
            upstream=None,
            error_code="vendored_contract_invalid",
            detail_code=exc.code,
        )

    vendored = _artifact_identity(vendored_artifact)
    try:
        body = fetcher(
            repository=repository,
            ref=ref,
            path=path,
            timeout=timeout,
            attempts=attempts,
            retry_delay=retry_delay,
        )
    except FreshnessError as exc:
        return _evidence(
            classification="unknown",
            retryable=exc.retryable,
            repository=repository,
            ref=ref,
            path=path,
            vendored=vendored,
            upstream=None,
            error_code=exc.code,
        )
    except (
        LaunchplaneSafetyError,
        http.client.HTTPException,
        OSError,
        TimeoutError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ):
        return _evidence(
            classification="unknown",
            retryable=True,
            repository=repository,
            ref=ref,
            path=path,
            vendored=vendored,
            upstream=None,
            error_code="upstream_unavailable",
        )

    try:
        upstream_artifact = json.loads(body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _evidence(
            classification="unknown",
            retryable=False,
            repository=repository,
            ref=ref,
            path=path,
            vendored=vendored,
            upstream=None,
            error_code="upstream_json_invalid",
        )
    if not isinstance(upstream_artifact, dict):
        return _evidence(
            classification="unknown",
            retryable=False,
            repository=repository,
            ref=ref,
            path=path,
            vendored=vendored,
            upstream=None,
            error_code="upstream_contract_invalid",
            detail_code="invalid_artifact",
        )
    try:
        return compare_artifacts(
            vendored_artifact,
            upstream_artifact,
        )
    except ContractError as exc:
        return _evidence(
            classification="unknown",
            retryable=False,
            repository=repository,
            ref=ref,
            path=path,
            vendored=vendored,
            upstream=None,
            error_code="upstream_contract_invalid",
            detail_code=exc.code,
        )


def maintenance_issue_body(evidence: Mapping[str, Any]) -> str:
    if evidence.get("classification") != "known-stale":
        raise FreshnessError("maintenance_issue_requires_known_stale")
    vendored = evidence.get("vendored")
    upstream = evidence.get("upstream")
    source = evidence.get("source")
    if not all(isinstance(value, Mapping) for value in (vendored, upstream, source)):
        raise FreshnessError("invalid_freshness_evidence")
    source_identity = (
        source.get("repository"),
        source.get("ref"),
        source.get("path"),
    )
    if source_identity != (
        DEFAULT_UPSTREAM_REPOSITORY,
        DEFAULT_UPSTREAM_REF,
        DEFAULT_UPSTREAM_PATH,
    ):
        raise FreshnessError("invalid_freshness_evidence")
    for identity in (vendored, upstream):
        if not isinstance(identity.get("schema_version"), int):
            raise FreshnessError("invalid_freshness_evidence")
        if not isinstance(identity.get("normalization_version"), int):
            raise FreshnessError("invalid_freshness_evidence")
        digest = identity.get("semantic_digest_sha256")
        source_sha = identity.get("source_commit_sha")
        if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
            raise FreshnessError("invalid_freshness_evidence")
        if not isinstance(source_sha, str) or not _SOURCE_SHA_PATTERN.fullmatch(
            source_sha
        ):
            raise FreshnessError("invalid_freshness_evidence")
    vendored_digest_line = (
        f"- Vendored semantic digest: `{vendored['semantic_digest_sha256']}`"
    )
    upstream_digest_line = (
        f"- Upstream semantic digest: `{upstream['semantic_digest_sha256']}`"
    )
    upstream_provenance_line = (
        f"- Upstream source provenance: `{upstream['source_commit_sha']}`"
    )
    return "\n".join(
        (
            MAINTENANCE_ISSUE_MARKER,
            "## Launchplane contract semantic drift",
            "",
            (
                "The scheduled advisory comparison found a valid upstream contract "
                "whose normalized semantic digest differs from the vendored artifact."
            ),
            "",
            "## Evidence",
            "",
            "- Classification: `known-stale`",
            f"- Source repository: `{source['repository']}`",
            f"- Source ref: `{source['ref']}`",
            f"- Source path: `{source['path']}`",
            f"- Vendored schema version: `{vendored['schema_version']}`",
            f"- Upstream schema version: `{upstream['schema_version']}`",
            (
                "- Vendored normalization version: "
                f"`{vendored['normalization_version']}`"
            ),
            (
                "- Upstream normalization version: "
                f"`{upstream['normalization_version']}`"
            ),
            vendored_digest_line,
            upstream_digest_line,
            upstream_provenance_line,
            "",
            "## Next Action",
            "",
            (
                "Revalidate the producer artifact, update the vendored artifact and "
                "local conformance expectations in a focused PR, and keep this "
                "evidence advisory. It is not runtime authority and must not block "
                "ordinary Launchplane helper reads."
            ),
            "",
            (
                "See `launchplane/references/agent-operator-contract.md` for the "
                "consumer update contract."
            ),
        )
    )


def _run_json_command(args: list[str], stdin: str | None) -> dict[str, Any]:
    try:
        result = subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise FreshnessError("github_reporting_failed") from exc
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FreshnessError("github_reporting_failed") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise FreshnessError("github_reporting_failed")
    return payload


def _issue_number(payload: Mapping[str, Any]) -> int:
    issue = payload.get("issue")
    if isinstance(issue, Mapping) and isinstance(issue.get("number"), int):
        return issue["number"]
    if isinstance(payload.get("number"), int):
        return payload["number"]
    raise FreshnessError("github_reporting_failed")


def report_maintenance_issue(
    evidence: Mapping[str, Any],
    *,
    repository: str,
    runner: CommandRunner = _run_json_command,
) -> dict[str, Any]:
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise FreshnessError("invalid_reporting_repository")
    if evidence.get("classification") != "known-stale":
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "classification": evidence.get("classification"),
            "issue": {"action": "none", "number": None},
        }
    body = maintenance_issue_body(evidence)
    search_payload = runner(
        [
            sys.executable,
            str(GH_PLAN_HELPER),
            "--repo",
            repository,
            "search",
            f'"{MAINTENANCE_ISSUE_KEY}" in:body',
            "--state",
            "open",
            "--limit",
            "20",
        ],
        None,
    )
    issues = search_payload.get("issues")
    if not isinstance(issues, list):
        raise FreshnessError("github_reporting_failed")
    matches = [
        issue
        for issue in issues
        if isinstance(issue, Mapping)
        and issue.get("repo") == repository
        and issue.get("state") == "OPEN"
    ]
    if len(matches) > 1:
        raise FreshnessError("duplicate_maintenance_issues")
    if matches:
        number = matches[0].get("number")
        if not isinstance(number, int):
            raise FreshnessError("github_reporting_failed")
        payload = runner(
            [
                sys.executable,
                str(GH_ISSUE_HELPER),
                "edit",
                "-R",
                repository,
                "-t",
                MAINTENANCE_ISSUE_TITLE,
                str(number),
            ],
            body,
        )
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "classification": "known-stale",
            "issue": {"action": "updated", "number": _issue_number(payload)},
        }
    payload = runner(
        [
            sys.executable,
            str(GH_ISSUE_HELPER),
            "create",
            "-R",
            repository,
            MAINTENANCE_ISSUE_TITLE,
        ],
        body,
    )
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "classification": "known-stale",
        "issue": {"action": "created", "number": _issue_number(payload)},
    }
