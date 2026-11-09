#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import http.client
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import urllib.error

SCRIPT_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "acceptance_operator", SCRIPT_DIR / "launchplane-write-action.py"
)
assert spec is not None and spec.loader is not None
operator: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)

CLIENT = "client"
DIRECTOR_STANDING = "director_standing"
NO_CLIENT = "no_client"
UNKNOWN = "unknown"
READ_FAILURES = (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException)


@dataclass(frozen=True)
class ClientRelease:
    state: str
    reason: str = ""


@dataclass(frozen=True)
class ProductionCommit:
    commit: str = ""
    reason: str = ""


class UnsettledRead(Exception):
    pass


def repository_key(value: str) -> str:
    raw = value.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if raw.casefold().startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.strip("/").casefold()


def _read(settings: dict[str, Any], path: str, *, timeout: int = 10) -> dict[str, Any]:
    return operator.request_launchplane_read(
        service_url=settings["service_url"], settings=settings, path=path, query={}, timeout=timeout,
    )


def _repository_entries(settings: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = _read(settings, operator.helper_command_path("repo-product-mapping-read")).get("mapping")
    if not isinstance(mapping, dict) or not isinstance(mapping.get("repositories"), list):
        raise UnsettledRead("Launchplane's repository mapping is malformed")
    ambiguous = mapping.get("ambiguous_repositories", [])
    if not isinstance(ambiguous, list) or ambiguous:
        raise UnsettledRead("Launchplane maps a repository to more than one product")
    entries = mapping["repositories"]
    if not all(
        isinstance(entry, dict)
        and isinstance(entry.get("repository"), str)
        and (entry.get("product") or entry.get("source") == "every_code_work_request")
        for entry in entries
    ):
        raise UnsettledRead("Launchplane's repository mapping is malformed")
    return entries


def _client_profile(settings: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any] | None:
    if not entry.get("product"):
        return None
    product = operator.public_identifier(entry["product"])
    payload = _read(settings, operator.helper_command_path("product-profile-read").format(product=product))
    summary = operator.summarize_product_profile_read(request={"product": product}, provider_payload=payload)
    result = summary["result"]
    raw = payload.get("profile")
    if (
        _omits_required_profile_fields(raw)
        or summary["status"] != "ok"
        or result["product"] != product
        or not isinstance(result.get("repository"), str)
        or repository_key(result["repository"]) != repository_key(entry["repository"])
        or result.get("lifecycle_state") != "active"
    ):
        raise UnsettledRead("Launchplane's product record does not match its repository mapping")
    if not result["owner_github_login"]:
        return None
    return {**result, "client_is_director": payload.get("client_is_director") is True}


def _omits_required_profile_fields(raw_profile: object) -> bool:
    if not isinstance(raw_profile, dict) or not ({"client", "owner"} & raw_profile.keys()):
        return True
    client = raw_profile["client"] if "client" in raw_profile else raw_profile["owner"]
    return client is not None and not (isinstance(client, dict) and "github_login" in client)


def _director_accepts(profile: dict[str, Any]) -> bool:
    return profile["client_is_director"] or profile["release_on_acceptance"] == DIRECTOR_STANDING


def _settled_release(settings: dict[str, Any], target_key: str) -> str:
    entries = _repository_entries(settings)
    matches = [entry for entry in entries if repository_key(entry["repository"]) == target_key]
    if len(matches) > 1:
        raise UnsettledRead("Launchplane maps the repository more than once")
    profiles = [
        profile for entry in matches if (profile := _client_profile(settings, entry)) is not None
    ]
    if not all(_director_accepts(profile) for profile in profiles):
        return CLIENT
    return DIRECTOR_STANDING if profiles else NO_CLIENT


def _settings() -> dict[str, Any]:
    settings = operator.resolve_settings(operator.parse_args(["product-profile-read", "--product", "unused"]))
    operator.validate_service_url(settings["service_url"])
    if not settings["token"]:
        raise UnsettledRead("no Launchplane operator token is configured")
    return settings


def read_client_release(repository: str) -> ClientRelease:
    target_key = repository_key(repository)
    if target_key.count("/") != 1:
        return ClientRelease(UNKNOWN, "the repository is not named as owner/name")
    try:
        return ClientRelease(_settled_release(_settings(), target_key))
    except UnsettledRead as unsettled:
        return ClientRelease(UNKNOWN, str(unsettled))
    except READ_FAILURES:
        # Provider bodies, private config and exception text never reach GitHub.
        return ClientRelease(UNKNOWN, "the Launchplane read failed")


def read_production_commit(repository: str) -> ProductionCommit:
    target_key = repository_key(repository)
    try:
        settings = _settings()
        matches = [entry for entry in _repository_entries(settings) if repository_key(entry["repository"]) == target_key]
        if len(matches) != 1 or not matches[0].get("product"):
            return ProductionCommit(reason="Launchplane maps no single product to this repository")
        product = operator.public_identifier(matches[0]["product"])
        path = operator.helper_command_path("product-promotion-status-read").format(product=product, environment="prod")
        status = _read(settings, path, timeout=30).get("promotion_status")
        destination = status.get("destination") if isinstance(status, dict) else None
        commit = destination.get("source_git_ref") if isinstance(destination, dict) else None
        if not isinstance(commit, str) or not commit:
            return ProductionCommit(reason="Launchplane reports no deployed production commit")
        return ProductionCommit(commit=commit)
    except UnsettledRead as unsettled:
        return ProductionCommit(reason=str(unsettled))
    except READ_FAILURES:
        return ProductionCommit(reason="the Launchplane read failed")
