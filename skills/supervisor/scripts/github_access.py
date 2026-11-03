# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))

github_identity = importlib.import_module("github_identity")
github_read = importlib.import_module("github_read")


def automation_configured() -> bool:
    values = {**os.environ, **github_identity.load_local_env()}
    names = {
        "CODEX_AUTOMATION_LOGIN",
        "GH_WITH_ENV_TOKEN_EXPECTED_LOGIN",
        "CODEX_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }
    return any(
        value and (name in names or re.fullmatch(r"GITHUB_(?:READER_|CLIENT_)?APP_.*", name))
        for name, value in values.items()
    ) or str(values.get("GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH", "")).casefold() in {"1", "yes", "true"}


def select_login(login: str | None = None) -> str | None:
    if automation_configured():
        if login:
            raise ValueError("CLI login selection conflicts with configured automation; preserve that identity")
        reader = github_read.GitHubReader(
            operation="github.supervisor.foreign_watch",
            strict_actor=True,
            gh_prefix_args=github_read.automation_only_gh_prefix_args(),
            deadline_at=time.time() + 60,
        )
        try:
            reader.get_json("/user", step="supervisor_auth_preflight")
        except (github_read.GitHubReadError, github_read.GitHubReadShapeError) as error:
            raise ValueError(
                "configured GitHub identity unavailable; inspect its authentication before starting"
            ) from error
        return None
    env = {key: value for key, value in os.environ.items() if key != github_identity.OWN_USER_OPT_IN}
    env["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"] = "1"
    wrapper = Path(__file__).resolve().parents[2] / "github/scripts/gh-with-env-token"
    result = subprocess.run(
        [str(wrapper), "api", "user", "--jq", ".login"], env=env, capture_output=True, text=True, timeout=60
    )
    actual = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", actual):
        raise ValueError("GitHub CLI login unavailable; inspect gh auth status before starting")
    if login and actual.casefold() != login.casefold():
        raise ValueError("GitHub CLI account changed; preserve the selected login")
    os.environ["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"] = "1"
    return actual


def reader_options(login: str | None = None) -> dict[str, Any]:
    if login:
        return {"strict_actor": True, "actor": login, "expected_actor": login, "gh_prefix_args": []}
    return {"strict_actor": True, "gh_prefix_args": github_read.automation_only_gh_prefix_args()}


def write_environment(login: str | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != github_identity.OWN_USER_OPT_IN}
    if login:
        env.update(GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK="1", GH_WITH_ENV_TOKEN_EXPECTED_LOGIN=login)
    return env
