#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Small shared identity/configuration helpers for GitHub skills."""

from __future__ import annotations

import os
import pathlib
import shlex
from collections.abc import Mapping


def env_file_path(environ: Mapping[str, str] | None = None) -> pathlib.Path | None:
    values = os.environ if environ is None else environ
    if values.get("CODEX_SKILLS_ENV_FILE"):
        return pathlib.Path(values["CODEX_SKILLS_ENV_FILE"]).expanduser()
    if values.get("CODE_HOME"):
        return pathlib.Path(values["CODE_HOME"]).expanduser() / "local.env"
    if values.get("CODEX_HOME"):
        return pathlib.Path(values["CODEX_HOME"]).expanduser() / "local.env"
    if values.get("HOME"):
        return pathlib.Path(values["HOME"]).expanduser() / ".code" / "local.env"
    return None


def load_local_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    path = env_file_path(environ)
    if path is None or not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            continue
        try:
            parsed = shlex.split(raw_value, comments=True, posix=True)
        except ValueError:
            continue
        if len(parsed) > 1:
            continue
        values[key] = parsed[0] if parsed else ""
    return values


def configured_value(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    local_env: Mapping[str, str] | None = None,
) -> str | None:
    values = os.environ if environ is None else environ
    local_values = local_env if local_env is not None else load_local_env(values)
    merged = dict(values)
    merged.update(local_values)
    value = merged.get(name)
    value = str(value or "").strip()
    return value or None


def automation_login(environ: Mapping[str, str] | None = None) -> str | None:
    values = os.environ if environ is None else environ
    local_values = load_local_env(values)
    return configured_value(
        "GH_WITH_ENV_TOKEN_EXPECTED_LOGIN",
        environ=values,
        local_env=local_values,
    ) or configured_value(
        "CODEX_AUTOMATION_LOGIN",
        environ=values,
        local_env=local_values,
    )


def automation_email(environ: Mapping[str, str] | None = None) -> str | None:
    values = os.environ if environ is None else environ
    local_values = load_local_env(values)
    return configured_value("CODEX_AUTOMATION_EMAIL", environ=values, local_env=local_values)


def commit_name(environ: Mapping[str, str] | None = None) -> str | None:
    values = os.environ if environ is None else environ
    local_values = load_local_env(values)
    return configured_value("GIT_COMMIT_AS_BOT_NAME", environ=values, local_env=local_values) or configured_value(
        "CODEX_AUTOMATION_LOGIN",
        environ=values,
        local_env=local_values,
    )


def commit_email(environ: Mapping[str, str] | None = None) -> str | None:
    values = os.environ if environ is None else environ
    local_values = load_local_env(values)
    return configured_value("GIT_COMMIT_AS_BOT_EMAIL", environ=values, local_env=local_values) or configured_value(
        "CODEX_AUTOMATION_EMAIL",
        environ=values,
        local_env=local_values,
    )


def configured_bot_logins(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    raw = configured_value("CODEX_AUTOMATION_BOT_LOGINS", environ=environ)
    if not raw:
        return ()
    return tuple(dict.fromkeys(item.strip() for item in raw.replace(",", " ").split() if item.strip()))


def active_auth_fallback_allowed(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    if str(values.get("GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH") or "").casefold() in {
        "1",
        "true",
        "yes",
    }:
        return False
    local_values = load_local_env(values)
    merged = dict(values)
    merged.update(local_values)
    if str(merged.get("GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH") or "").casefold() in {
        "1",
        "true",
        "yes",
    }:
        return False
    raw = merged.get("GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK")
    return str(raw or "").casefold() in {
        "1",
        "true",
        "yes",
    }


def automation_identity_configured(environ: Mapping[str, str] | None = None) -> bool:
    return automation_login(environ) is not None
