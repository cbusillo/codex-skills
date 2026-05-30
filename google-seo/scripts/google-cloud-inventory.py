#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read-only Google Cloud project inventory for SEO/API tooling triage."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any


REDACTED = "[redacted]"
SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "clientsecret",
    "key_string",
    "keystring",
    "password",
    "private_key",
    "privatekey",
    "refresh_token",
    "refreshtoken",
    "secret",
    "token",
}


def normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def redacted(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if normalized_key(key) in SENSITIVE_KEYS else redacted(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return value


def public_project(project: dict[str, Any]) -> dict[str, Any]:
    billing = project.get("billing")
    return {
        "projectId": project.get("projectId"),
        "billingEnabled": billing.get("billingEnabled")
        if isinstance(billing, dict)
        else None,
        "enabledServices": project.get("enabledServices"),
        "serviceAccounts": project.get("serviceAccounts"),
        "apiKeys": project.get("apiKeys"),
        "iamBindingCount": len(project.get("iamBindings", []))
        if isinstance(project.get("iamBindings"), list)
        else None,
    }


def run_gcloud(args: list[str], *, allow_failure: bool = False) -> Any:
    command = ["gcloud", *args]
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        if allow_failure:
            return {
                "ok": False,
                "command": command,
                "stderr": completed.stderr.strip(),
            }
        print(completed.stderr.strip(), file=sys.stderr)
        raise SystemExit(completed.returncode)
    if not completed.stdout.strip():
        return []
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return completed.stdout.strip()


def active_account() -> str | None:
    completed = subprocess.run(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip() or None


def api_key_details(project_id: str) -> list[dict[str, Any]]:
    keys = run_gcloud(
        ["services", "api-keys", "list", "--project", project_id, "--format=json"],
        allow_failure=True,
    )
    if isinstance(keys, dict) and keys.get("ok") is False:
        return [keys]

    details: list[dict[str, Any]] = []
    for key in keys:
        name = key.get("name")
        if not name:
            continue
        detail = run_gcloud(
            [
                "services",
                "api-keys",
                "describe",
                name,
                "--project",
                project_id,
                "--format=json",
            ],
            allow_failure=True,
        )
        if isinstance(detail, dict):
            details.append(
                {
                    "displayName": detail.get("displayName"),
                    "uid": detail.get("uid"),
                    "createTime": detail.get("createTime"),
                    "updateTime": detail.get("updateTime"),
                    "restrictions": detail.get("restrictions"),
                }
            )
    return details


def project_inventory(project_id: str) -> dict[str, Any]:
    services = run_gcloud(
        ["services", "list", "--enabled", "--project", project_id, "--format=json"],
        allow_failure=True,
    )
    service_accounts = run_gcloud(
        ["iam", "service-accounts", "list", "--project", project_id, "--format=json"],
        allow_failure=True,
    )
    billing = run_gcloud(
        ["billing", "projects", "describe", project_id, "--format=json"],
        allow_failure=True,
    )
    iam = run_gcloud(
        ["projects", "get-iam-policy", project_id, "--format=json"],
        allow_failure=True,
    )
    return {
        "projectId": project_id,
        "billing": billing,
        "enabledServices": [
            item.get("config", {}).get("name")
            for item in services
            if isinstance(item, dict)
        ]
        if isinstance(services, list)
        else services,
        "serviceAccounts": [
            {
                "email": item.get("email"),
                "displayName": item.get("displayName"),
                "disabled": item.get("disabled"),
            }
            for item in service_accounts
            if isinstance(item, dict)
        ]
        if isinstance(service_accounts, list)
        else service_accounts,
        "apiKeys": api_key_details(project_id),
        "iamBindings": iam.get("bindings", []) if isinstance(iam, dict) else iam,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", help="project id to inspect")
    args = parser.parse_args()

    projects = args.project or [
        item["projectId"]
        for item in run_gcloud(["projects", "list", "--format=json"])
        if item.get("lifecycleState") == "ACTIVE"
    ]
    payload = {
        "activeAccount": active_account(),
        "projects": [
            public_project(project_inventory(project_id)) for project_id in projects
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
