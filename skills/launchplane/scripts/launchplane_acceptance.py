#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read whether Launchplane ties a repository to a Client, without granting release authority."""

import http.client
import importlib.util
import os
import sys
from collections.abc import Iterator
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
    """One of CLIENT, DIRECTOR_STANDING, NO_CLIENT or UNKNOWN, with a public-safe reason."""

    state: str
    reason: str = ""


class UnsettledRead(Exception):
    """Launchplane evidence that cannot settle whether a repository has a Client."""


def repository_key(value: str) -> str:
    raw = value.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if raw.casefold().startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.strip("/").casefold()


def launchplane_configuration() -> str:
    """The github skill's host check: "not_configured" only when this host has no Launchplane at all."""
    sys.path.insert(0, str(SCRIPT_DIR.parents[2]))
    from skills.github.scripts import github_launchplane

    return github_launchplane.configuration_status(
        {}, home=Path.home(), environ=os.environ, expected_home=github_launchplane.director_home()
    )


def _read(settings: dict[str, Any], path: str) -> dict[str, Any]:
    return operator.request_launchplane_read(
        service_url=settings["service_url"], settings=settings, path=path, query={}, timeout=10,
    )


def _repository_entries(settings: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = _read(settings, operator.helper_command_path("repo-product-mapping-read")).get("mapping")
    if not isinstance(mapping, dict) or not isinstance(mapping.get("repositories"), list):
        raise UnsettledRead("Launchplane's repository mapping is malformed")
    ambiguous = mapping.get("ambiguous_repositories", [])
    if not isinstance(ambiguous, list) or ambiguous:
        # An ambiguous product's lanes are hidden from the mapping, so its pins cannot be checked.
        raise UnsettledRead("Launchplane maps a repository to more than one product")
    entries = mapping["repositories"]
    if not all(isinstance(entry, dict) and isinstance(entry.get("repository"), str) for entry in entries):
        raise UnsettledRead("Launchplane's repository mapping is malformed")
    return entries


def _client_profile(settings: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any] | None:
    """The mapped product's profile when it has a Client; None when the entry names no Client."""
    if not entry.get("product"):
        return None
    product = operator.public_identifier(entry["product"])
    summary = operator.summarize_product_profile_read(
        request={"product": product},
        provider_payload=_read(settings, operator.helper_command_path("product-profile-read").format(product=product)),
    )
    result = summary["result"]
    if (
        summary["status"] != "ok"
        or result["product"] != product
        or not isinstance(result.get("repository"), str)
        or repository_key(result["repository"]) != repository_key(entry["repository"])
        # The mapping lists only active products; any other state is a moving or broken record.
        or result.get("lifecycle_state") != "active"
    ):
        raise UnsettledRead("Launchplane's product record does not match its repository mapping")
    return result if result["owner_github_login"] else None


def _lane_pins(settings: dict[str, Any], *, product: str, instance: str, target_key: str) -> bool:
    payload = _read(
        settings,
        operator.helper_command_path("product-environment-read").format(product=product, environment=instance),
    )
    summary = operator.summarize_product_environment_read(
        request={"product": product, "environment": instance}, provider_payload=payload,
    )
    result = summary["result"]
    if summary["status"] != "ok" or result["product"] != product or result["environment"] != instance:
        raise UnsettledRead("Launchplane's lane record does not match its product")
    environment = payload.get("environment")
    target = environment.get("target") if isinstance(environment, dict) else None
    manifest = target.get("artifact_manifest") if isinstance(target, dict) else None
    sources = manifest.get("addon_sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, list) or not all(
        isinstance(source, dict) and isinstance(source.get("repository"), str) for source in sources
    ):
        raise UnsettledRead("Launchplane's addon sources for an Odoo lane are unreadable")
    target_name = target_key.split("/", 1)[1]
    keys = {repository_key(source["repository"]) for source in sources}
    # A source recorded without its owner may be this repository under the same name.
    return target_key in keys or target_name in keys


def _pinned_release_modes(
    settings: dict[str, Any], entries: list[dict[str, Any]], target_key: str
) -> Iterator[str]:
    """Release modes of Client products whose prod or testing lane pins the repository as an addon source."""
    for entry in entries:
        if entry.get("driver_id") != "odoo":
            continue
        profile = _client_profile(settings, entry)
        if profile is None:
            continue
        if profile.get("driver_id") != "odoo" or profile.get("lanes_truncated"):
            raise UnsettledRead("Launchplane's lane list for an Odoo product is incomplete")
        instances = dict.fromkeys(
            str(lane["instance"]) for lane in profile.get("lanes") or []
            if isinstance(lane, dict) and lane.get("instance") in {"prod", "testing"}
        )
        if any(
            _lane_pins(settings, product=str(profile["product"]), instance=instance, target_key=target_key)
            for instance in instances
        ):
            yield profile["release_on_acceptance"]


def _settled_release(settings: dict[str, Any], target_key: str) -> str:
    entries = _repository_entries(settings)
    matches = [entry for entry in entries if repository_key(entry["repository"]) == target_key]
    if len(matches) > 1:
        raise UnsettledRead("Launchplane maps the repository more than once")
    modes = [
        profile["release_on_acceptance"]
        for entry in matches
        if (profile := _client_profile(settings, entry)) is not None
    ]
    if any(mode != DIRECTOR_STANDING for mode in modes):
        return CLIENT
    for mode in _pinned_release_modes(settings, entries, target_key):
        if mode != DIRECTOR_STANDING:
            return CLIENT
        modes.append(mode)
    return DIRECTOR_STANDING if modes else NO_CLIENT


def read_client_release(repository: str) -> ClientRelease:
    """Whether Launchplane maps the repository, directly or as a pinned Odoo addon source, to an
    active product with a Client. A Client whose product releases on the Director's standing
    acceptance reads as DIRECTOR_STANDING. Any evidence that cannot settle it reads as UNKNOWN."""
    target_key = repository_key(repository)
    if target_key.count("/") != 1:
        return ClientRelease(UNKNOWN, "the repository is not named as owner/name")
    try:
        if launchplane_configuration() == "not_configured":
            return ClientRelease(NO_CLIENT, "this host has no Launchplane, so no product maps the repository")
        settings = operator.resolve_settings(operator.parse_args(["product-profile-read", "--product", "unused"]))
        operator.validate_service_url(settings["service_url"])
        if not settings["token"]:
            return ClientRelease(UNKNOWN, "no Launchplane operator token is configured")
        return ClientRelease(_settled_release(settings, target_key))
    except UnsettledRead as unsettled:
        return ClientRelease(UNKNOWN, str(unsettled))
    except READ_FAILURES:
        # Provider bodies, private config and exception text never reach GitHub.
        return ClientRelease(UNKNOWN, "the Launchplane read failed")
