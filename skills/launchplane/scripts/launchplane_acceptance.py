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


def read_director_standing(repository: str) -> bool:
    """Unknown evidence preserves the Client acceptance path. Explicit PR holds
    are handled by the caller before this optional exemption is considered."""
    try:
        args = operator.parse_args(["product-profile-read", "--product", "unused"])
        settings = operator.resolve_settings(args)
        operator.validate_service_url(settings["service_url"])
        if not settings["token"]:
            return False
        mapping_payload = operator.request_launchplane_read(
            service_url=settings["service_url"], settings=settings,
            path=operator.helper_command_path("repo-product-mapping-read"),
            query={}, timeout=10,
        )
        mapping = mapping_payload.get("mapping")
        if not isinstance(mapping, dict) or not isinstance(mapping.get("repositories"), list):
            return False
        matches = [
            entry for entry in mapping["repositories"]
            if isinstance(entry, dict) and isinstance(entry.get("repository"), str)
            and entry["repository"].casefold() == repository.casefold()
        ]
        if len(matches) != 1 or matches[0].get("classification") != "managed_runtime":
            return False
        product = operator.public_identifier(matches[0].get("product"))
        profile_payload = operator.request_launchplane_read(
            service_url=settings["service_url"], settings=settings,
            path=operator.helper_command_path("product-profile-read").format(product=product),
            query={}, timeout=10,
        )
        profile = operator.summarize_product_profile_read(
            request={"product": product}, provider_payload=profile_payload
        )
        result = profile["result"]
        return bool(
            profile["status"] == "ok"
            and result["product"] == product
            and result["repository"].casefold() == repository.casefold()
            and result["owner_github_login"]
            and result["release_on_acceptance"] == "director_standing"
        )
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
        # Provider bodies, private config and exception text never reach GitHub.
        return False
