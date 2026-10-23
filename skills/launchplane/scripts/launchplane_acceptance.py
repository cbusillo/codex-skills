#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read standing acceptance for a repository without granting release authority."""

import http.client
import importlib.util
from collections.abc import Iterator
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


def repository_key(value: str) -> str:
    raw = value.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if raw.casefold().startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.strip("/").casefold()


def _repository_entries(
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool] | None:
    mapping_payload = operator.request_launchplane_read(
        service_url=settings["service_url"], settings=settings,
        path=operator.helper_command_path("repo-product-mapping-read"),
        query={}, timeout=10,
    )
    mapping = mapping_payload.get("mapping")
    if not isinstance(mapping, dict) or not isinstance(mapping.get("repositories"), list):
        return None
    ambiguous = mapping.get("ambiguous_repositories", [])
    if not isinstance(ambiguous, list):
        return None
    entries = [
        entry for entry in mapping["repositories"]
        if isinstance(entry, dict)
        and isinstance(entry.get("repository"), str)
    ]
    return entries, bool(ambiguous)


def _read_active_profile(
    settings: dict[str, Any], entry: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        product = operator.public_identifier(entry.get("product"))
        profile_payload = operator.request_launchplane_read(
            service_url=settings["service_url"], settings=settings,
            path=operator.helper_command_path("product-profile-read").format(product=product),
            query={}, timeout=10,
        )
        profile = operator.summarize_product_profile_read(
            request={"product": product}, provider_payload=profile_payload
        )
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
        return None
    result = profile["result"]
    if (
        profile["status"] != "ok"
        or result["product"] != product
        or not isinstance(result.get("repository"), str)
        or repository_key(result["repository"]) != repository_key(entry["repository"])
        or result.get("lifecycle_state") != "active"
    ):
        return None
    return result


def _lane_pins_addon_source(
    settings: dict[str, Any], *, product: str, instance: str, target_key: str
) -> bool | None:
    try:
        env_payload = operator.request_launchplane_read(
            service_url=settings["service_url"], settings=settings,
            path=operator.helper_command_path("product-environment-read").format(
                product=product, environment=instance
            ),
            query={}, timeout=10,
        )
        summary = operator.summarize_product_environment_read(
            request={"product": product, "environment": instance},
            provider_payload=env_payload,
        )
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
        return None
    env_result = summary["result"]
    if (
        summary["status"] != "ok"
        or env_result["product"] != product
        or env_result["environment"] != instance
    ):
        return None
    environment = env_payload.get("environment")
    target = environment.get("target") if isinstance(environment, dict) else None
    manifest = target.get("artifact_manifest") if isinstance(target, dict) else None
    sources = manifest.get("addon_sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, list):
        return None
    if any(
        isinstance(source, dict)
        and isinstance(source.get("repository"), str)
        and repository_key(source["repository"]) == target_key
        for source in sources
    ):
        return True
    if any(
        not isinstance(source, dict)
        or not isinstance(source.get("repository"), str)
        or "/" not in repository_key(source["repository"])
        for source in sources
    ):
        return None
    return False


def _pinned_release_modes(settings: dict[str, Any], entries: list[dict[str, Any]], target_key: str) -> Iterator[str | None]:
    for entry in entries:
        if entry.get("driver_id") != "odoo":
            continue
        profile = _read_active_profile(settings, entry)
        if profile is None:
            yield None
            continue
        if profile.get("driver_id") != "odoo":
            yield None
            continue
        if profile.get("lanes_truncated"):
            yield None
        for instance in dict.fromkeys(
            str(lane["instance"])
            for lane in (profile.get("lanes") or [])
            if isinstance(lane, dict) and lane.get("instance") in {"prod", "testing"}
        ):
            pinned = _lane_pins_addon_source(settings, product=str(profile["product"]), instance=instance, target_key=target_key)
            if pinned is None:
                yield None
            elif pinned:
                yield profile["release_on_acceptance"] if profile["owner_github_login"] else None


def read_director_standing(repository: str) -> bool:
    """Unknown evidence preserves the Client acceptance path. Explicit PR holds
    are handled by the caller before this optional exemption is considered."""
    try:
        target_key = repository_key(repository)
        if not target_key or "/" not in target_key:
            return False
        args = operator.parse_args(["product-profile-read", "--product", "unused"])
        settings = operator.resolve_settings(args)
        operator.validate_service_url(settings["service_url"])
        if not settings["token"]:
            return False
        mapping = _repository_entries(settings)
        if mapping is None:
            return False
        entries, ambiguous = mapping
        if ambiguous:
            return False
        matches = [
            entry for entry in entries
            if repository_key(entry["repository"]) == target_key
        ]
        if len(matches) != 1:
            return False
        result = _read_active_profile(settings, matches[0])
        return bool(
            result is not None
            and result["owner_github_login"]
            and result["release_on_acceptance"] == "director_standing"
            and all(mode == "director_standing" for mode in _pinned_release_modes(settings, entries, target_key))
        )
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
        # Provider bodies, private config and exception text never reach GitHub.
        return False


def read_client_release_repository(repository: str) -> bool:
    """Return True when Launchplane maps the repository or a pinned Odoo addon
    source to an active product with a Client login."""
    try:
        target_key = repository_key(repository)
        if not target_key or "/" not in target_key:
            return False
        args = operator.parse_args(["product-profile-read", "--product", "unused"])
        settings = operator.resolve_settings(args)
        operator.validate_service_url(settings["service_url"])
        if not settings["token"]:
            return False
        mapping = _repository_entries(settings)
        if mapping is None:
            return False
        entries, _ = mapping
        matches = [
            entry for entry in entries
            if repository_key(entry["repository"]) == target_key
        ]
        if len(matches) > 1:
            return False
        for entry in matches:
            profile = _read_active_profile(settings, entry)
            if profile is not None and profile["owner_github_login"]:
                return True
        return any(_pinned_release_modes(settings, entries, target_key))
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
        # Provider bodies, private config and exception text never reach GitHub.
        return False
