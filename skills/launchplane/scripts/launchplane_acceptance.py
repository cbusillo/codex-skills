#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read standing acceptance for a repository without granting release authority."""

import http.client
import importlib.util
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


def _managed_runtime_entries(
    settings: dict[str, Any],
) -> list[dict[str, Any]] | None:
    mapping_payload = operator.request_launchplane_read(
        service_url=settings["service_url"], settings=settings,
        path=operator.helper_command_path("repo-product-mapping-read"),
        query={}, timeout=10,
    )
    mapping = mapping_payload.get("mapping")
    if not isinstance(mapping, dict) or not isinstance(mapping.get("repositories"), list):
        return None
    return [
        entry for entry in mapping["repositories"]
        if isinstance(entry, dict)
        and entry.get("classification") == "managed_runtime"
        and isinstance(entry.get("repository"), str)
    ]


def _read_active_client_profile(
    settings: dict[str, Any], entry: dict[str, Any]
) -> dict[str, Any] | None:
    product = operator.public_identifier(entry.get("product"))
    profile_payload = operator.request_launchplane_read(
        service_url=settings["service_url"], settings=settings,
        path=operator.helper_command_path("product-profile-read").format(product=product),
        query={}, timeout=10,
    )
    profile = operator.summarize_product_profile_read(
        request={"product": product}, provider_payload=profile_payload
    )
    result = profile["result"]
    if (
        profile["status"] != "ok"
        or result["product"] != product
        or not isinstance(result.get("repository"), str)
        or repository_key(result["repository"]) != repository_key(entry["repository"])
        or result.get("lifecycle_state") in {"retired", "archived"}
        or not result["owner_github_login"]
    ):
        return None
    return result


def _lane_pins_addon_source(
    settings: dict[str, Any], *, product: str, instance: str, target_key: str
) -> bool:
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
        return False
    env_result = summary["result"]
    if (
        summary["status"] != "ok"
        or env_result["product"] != product
        or env_result["environment"] != instance
    ):
        return False
    environment = env_payload.get("environment")
    target = environment.get("target") if isinstance(environment, dict) else None
    manifest = target.get("artifact_manifest") if isinstance(target, dict) else None
    sources = manifest.get("addon_sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, list):
        return False
    return any(
        isinstance(source, dict)
        and isinstance(source.get("repository"), str)
        and repository_key(source["repository"]) == target_key
        for source in sources
    )


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
        managed = _managed_runtime_entries(settings)
        if managed is None:
            return False
        matches = [
            entry for entry in managed
            if repository_key(entry["repository"]) == target_key
        ]
        if len(matches) != 1:
            return False
        result = _read_active_client_profile(settings, matches[0])
        return bool(
            result is not None
            and result["release_on_acceptance"] == "director_standing"
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
        managed = _managed_runtime_entries(settings)
        if managed is None:
            return False
        matches = [
            entry for entry in managed
            if repository_key(entry["repository"]) == target_key
        ]
        if len(matches) > 1:
            return False
        if len(matches) == 1:
            return _read_active_client_profile(settings, matches[0]) is not None
        for entry in managed:
            driver_id = entry.get("driver_id")
            if isinstance(driver_id, str) and driver_id and driver_id != "odoo":
                continue
            result = _read_active_client_profile(settings, entry)
            if result is None or result.get("driver_id") != "odoo":
                continue
            product = str(result["product"])
            lanes = [
                str(lane["instance"])
                for lane in (result.get("lanes") or [])
                if isinstance(lane, dict) and lane.get("instance") in {"prod", "testing"}
            ] or ["prod", "testing"]
            for instance in dict.fromkeys(lanes):
                if _lane_pins_addon_source(
                    settings, product=product, instance=instance, target_key=target_key
                ):
                    return True
        return False
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
        # Provider bodies, private config and exception text never reach GitHub.
        return False
