#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Behavior checks that run skill helpers and commands.

Each check executes a helper or a command declared in skill metadata and
asserts what it does. Do not add checks that assert instruction wording; see
AGENTS.md.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_probe(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def launchplane_environment(**overrides: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LAUNCHPLANE_")
    }
    environment.update(overrides)
    return environment


def run_launchplane_payload_probe(
    helper: Path,
    payload_path: Path,
    fixture: Path,
    repo: Path,
) -> subprocess.CompletedProcess[str]:
    return run_probe(
        [
            sys.executable,
            str(helper),
            "--config",
            str(fixture / "missing-operator-config.json"),
            "product-config-dry-run",
            "--payload-file",
            str(payload_path),
        ],
        cwd=repo,
        env=launchplane_environment(),
    )


def validator_module() -> Any:
    path = ROOT / "skill-creator" / "scripts" / "validate-skill-repo.py"
    spec = importlib.util.spec_from_file_location("skill_repo_behavior_validator", path)
    if spec is None or spec.loader is None:
        raise AssertionError("Validator must be importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command_argv(skill_name: str, command_name: str) -> list[str]:
    metadata = validator_module().read_frontmatter(ROOT / skill_name / "SKILL.md")
    for command in metadata.get("commands", []):
        if command.get("name") == command_name:
            argv = command.get("example_argv")
            require(isinstance(argv, list) and bool(argv) and all(isinstance(token, str) for token in argv),
                    f"{command_name} must define a nonempty string example_argv list")
            return argv
    raise AssertionError(f"{skill_name} must define {command_name}")


def test_launchplane_write_action_helper_contract() -> None:
    with tempfile.TemporaryDirectory() as directory:
        fixture = Path(directory).resolve()
        repo = fixture / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        check_launchplane_write_action_helper_contract(fixture, repo)


def check_launchplane_write_action_helper_contract(fixture: Path, repo: Path) -> None:
    helper = ROOT / "launchplane" / "scripts" / "launchplane-write-action.py"
    no_context = run_probe(
        [
            sys.executable,
            str(helper),
            "--config",
            str(fixture / "missing-operator-config.json"),
            "merge-train-controller-run-once",
            "--repo",
            "example/repo",
            "--base-branch",
            "main",
        ],
        env=launchplane_environment(),
    )
    require(no_context.returncode == 2, "Write-action helper must fail closed without config")
    payload = json.loads(no_context.stdout)
    require(payload["status"] == "no_context", "Write-action helper must emit no_context")
    require(
        "missing_operator_config" in json.dumps(payload),
        "Write-action helper must explain missing operator config compactly",
    )

    missing_url = run_probe(
        [
            sys.executable,
            str(helper),
            "--env-config",
            str(fixture / "missing-operator.env"),
            "merge-train-controller-run-once",
            "--repo",
            "example/repo",
            "--base-branch",
            "main",
        ],
        env=launchplane_environment(
            LAUNCHPLANE_LOCAL_OPERATOR_TOKEN="secret-token-never-render",
        ),
    )
    require(missing_url.returncode == 2, "Write-action helper must fail closed without service URL")
    missing_url_payload = json.loads(missing_url.stdout)
    require(
        missing_url_payload["summary"]["configuration_state"] == "missing_service_url",
        "Write-action helper must distinguish a missing service URL from a missing credential",
    )

    missing_token = run_probe(
        [
            sys.executable,
            str(helper),
            "--env-config",
            str(fixture / "missing-operator.env"),
            "--url",
            "https://launchplane.example.invalid",
            "merge-train-controller-run-once",
            "--repo",
            "example/repo",
            "--base-branch",
            "main",
        ],
        env=launchplane_environment(),
    )
    require(missing_token.returncode == 2, "Write-action helper must fail closed without token")
    missing_token_payload = json.loads(missing_token.stdout)
    require(
        missing_token_payload["summary"]["configuration_state"] == "missing_operator_token",
        "Write-action helper must distinguish a missing token from missing operator config",
    )

    public_url_hint = run_probe(
        [
            sys.executable,
            str(helper),
            "--env-config",
            str(fixture / "missing-operator.env"),
            "operator-config-diagnostic",
        ],
        env=launchplane_environment(
            LAUNCHPLANE_PUBLIC_URL="https://public-launchplane.example.invalid",
            LAUNCHPLANE_LOCAL_OPERATOR_TOKEN="secret-token-never-render",
        ),
    )
    require(public_url_hint.returncode == 0, "Write-action diagnostic must tolerate public URL near-miss")
    public_url_payload = json.loads(public_url_hint.stdout)
    rendered_public_hint = json.dumps(public_url_payload)
    require(
        public_url_payload["status"] == "incomplete"
        and public_url_payload["summary"]["classification"] == "ambiguous_service_url",
        "Write-action diagnostic must classify LAUNCHPLANE_PUBLIC_URL without operator URL as ambiguous",
    )
    require(
        public_url_payload["summary"]["public_url_hint_present"] is True,
        "Write-action diagnostic must report a public URL hint without using it as authority",
    )
    require(
        "public-launchplane.example.invalid" not in rendered_public_hint
        and "secret-token-never-render" not in rendered_public_hint,
        "Write-action diagnostic must not render public URL hint or token values",
    )

    repo_payload = repo / "payload.json"
    try:
        repo_payload.write_text('{"reason":"example"}\n', encoding="utf-8")
        repo_local = run_launchplane_payload_probe(helper, repo_payload, fixture, repo)
    finally:
        repo_payload.unlink(missing_ok=True)
    require(repo_local.returncode == 2, "Write-action helper must reject repo-local payload files")
    repo_payload_error = json.loads(repo_local.stdout)
    require(
        "repo_local_payload_unsupported" in json.dumps(repo_payload_error),
        "Write-action helper must report repo-local payload rejection compactly",
    )

    with tempfile.TemporaryDirectory() as tmp:
        external_payload = Path(tmp) / "operator-payload.json"
        external_payload.write_text('{"reason":"external"}\n', encoding="utf-8")
        external_private = run_launchplane_payload_probe(helper, external_payload, fixture, repo)
        require(
            external_private.returncode == 2,
            "Write-action helper should reach missing-config handling for external payload files",
        )
        external_private_error = json.loads(external_private.stdout)
        require(
            "missing_operator_config" in json.dumps(external_private_error)
            and "repo_local_payload_unsupported" not in json.dumps(external_private_error),
            "Write-action helper must not reject external private payload files as repo-local",
        )
        repo_symlink = repo / "payload-link.json"
        try:
            repo_symlink.symlink_to(external_payload)
            symlink_local = run_launchplane_payload_probe(helper, repo_symlink, fixture, repo)
        finally:
            repo_symlink.unlink(missing_ok=True)
        require(
            symlink_local.returncode == 2,
            "Write-action helper must reject repo-local symlink payload files",
        )
        symlink_payload_error = json.loads(symlink_local.stdout)
        require(
            "repo_local_payload_unsupported" in json.dumps(symlink_payload_error),
            "Write-action helper must report repo-local symlink payload rejection compactly",
        )

    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / "local-operator.env"
        env_path.write_text(
            "LAUNCHPLANE_OPERATOR_URL=https://launchplane.example.invalid\n"
            "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN=secret-token-never-render\n"
            "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT=local-owner\n"
            "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL=local\n"
            "IGNORED_KEY=ignored\n",
            encoding="utf-8",
        )
        diagnostic = run_probe(
            [
                sys.executable,
                str(helper),
                "--env-config",
                str(env_path),
                "operator-config-diagnostic",
            ],
            env=launchplane_environment(),
        )
    require(diagnostic.returncode == 0, "Write-action helper diagnostic must succeed with private .env")
    diagnostic_payload = json.loads(diagnostic.stdout)
    rendered_diagnostic = json.dumps(diagnostic_payload)
    require(
        diagnostic_payload["summary"]["private_env_present"] is True,
        "Write-action helper diagnostic must report private .env presence",
    )
    require(
        diagnostic_payload["status"] == "available"
        and diagnostic_payload["summary"]["classification"] == "ready",
        "Write-action helper diagnostic must report ready only when URL and token are present",
    )
    require(
        diagnostic_payload["summary"]["token_source"] == "private_env",
        "Write-action helper diagnostic must report token source without value",
    )
    require(
        diagnostic_payload["summary"]["service_url_source"] == "private_env",
        "Write-action helper diagnostic must report winning service URL source without value",
    )
    require(
        "secret-token-never-render" not in rendered_diagnostic,
        "Write-action helper diagnostic must not render token values",
    )
    require(
        "local-owner" not in rendered_diagnostic and "https://launchplane.example.invalid" not in rendered_diagnostic,
        "Write-action helper diagnostic must not render subject or service URL values",
    )

    with tempfile.TemporaryDirectory() as tmp:
        json_path = Path(tmp) / "local-operator.json"
        env_path = Path(tmp) / "local-operator.env"
        json_path.write_text(
            json.dumps(
                {
                    "service_url": "https://json-config.example.invalid",
                    "operator_token_env": "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
                    "operator_subject_env": "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT",
                    "operator_token_label_env": "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL",
                }
            ),
            encoding="utf-8",
        )
        env_path.write_text(
            "LAUNCHPLANE_OPERATOR_URL=https://env-file.example.invalid\n"
            "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN=env-file-token-never-render\n",
            encoding="utf-8",
        )
        precedence = run_probe(
            [
                sys.executable,
                str(helper),
                "--config",
                str(json_path),
                "--env-config",
                str(env_path),
                "operator-config-diagnostic",
            ],
            env=launchplane_environment(
                LAUNCHPLANE_OPERATOR_URL="https://process-env.example.invalid",
            ),
        )
    require(precedence.returncode == 0, "Write-action helper diagnostic must accept --config with --env-config")
    precedence_payload = json.loads(precedence.stdout)
    precedence_summary = precedence_payload["summary"]
    rendered_precedence = json.dumps(precedence_payload)
    require(
        precedence_summary["service_url_source"] == "json_config",
        "Explicit --config service URL must win over ambient environment and explicit .env",
    )
    require(
        "private_env" not in precedence_summary["service_url_sources"],
        "Explicit --env-config must not be a service URL source when --config is set",
    )
    require(
        precedence_summary["token_source"] == "private_env",
        "Explicit --env-config must supply token values even when --config is set",
    )
    require(
        "process-env.example.invalid" not in rendered_precedence
        and "json-config.example.invalid" not in rendered_precedence
        and "env-file.example.invalid" not in rendered_precedence
        and "env-file-token-never-render" not in rendered_precedence,
        "Write-action helper precedence diagnostic must not render URL or token values",
    )

    spec = importlib.util.spec_from_file_location("launchplane_write_action", helper)
    require(spec is not None and spec.loader is not None, "Write-action helper must be importable")
    if spec is None or spec.loader is None:
        raise AssertionError("Write-action helper must be importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    success_payload = module.summarize_success(
        operation="merge-train-controller-run-once",
        request={"repository": "example/repo", "base_branch": "main", "mutate": False},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_example",
            "records": {"merge_train_batch_candidate_record_id": "candidate-example"},
            "result": {
                "repository": "example/repo",
                "base_branch": "main",
                "controller_action": "build_candidate",
            },
        },
    )
    rendered_success = json.dumps(success_payload)
    require(success_payload["status"] == "accepted", "Write-action success status must pass through")
    require(
        success_payload["summary"]["controller_action"] == "build_candidate",
        "Write-action success summary must expose controller action",
    )
    require("ghp_example" not in rendered_success, "Write-action success output must redact tokens")
    require("must-not-render" not in rendered_success, "Write-action success output must drop values")
    try:
        module.summarize_success(
            operation="merge-train-controller-run-once",
            request={"repository": "example/repo", "base_branch": "main", "mutate": False},
            provider_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_example",
                "records": {"merge_train_batch_candidate_record_id": "candidate-example"},
                "result": {
                    "repository": "example/repo",
                    "base_branch": "main",
                    "controller_action": "build_candidate",
                    "authorization": "Bearer ghp_example",
                },
            },
        )
    except ValueError as unsafe_shape_error:
        require(
            str(unsafe_shape_error) == "unsafe_response_shape",
            "Unsafe provider success shapes must fail closed",
        )
    else:
        raise AssertionError("Unsafe provider success shapes must fail closed")

    error_body = json.dumps(
        {
            "status": "rejected",
            "trace_id": "launchplane_req_denied",
            "error": {
                "code": "authorization_denied",
                "message": "Denied for Bearer secret-token-never-render at https://private-launchplane.example.invalid",
            },
        }
    ).encode()
    http_error = urllib.error.HTTPError(
        "https://launchplane.example.invalid/v1/example",
        403,
        "Forbidden",
        hdrs=Message(),
        fp=io.BytesIO(error_body),
    )
    denied_payload = module.summarize_http_error(
        operation="product-config-preflight",
        request={"product": "example-product", "context": "example-testing"},
        exc=http_error,
    )
    require(denied_payload["status"] == "denied", "Write-action 403 must summarize as denied")
    require(
        denied_payload["summary"]["error_code"] == "authorization_denied",
        "Write-action denied summary must expose safe error code",
    )
    rendered_denied = json.dumps(denied_payload)
    require(
        "secret-token-never-render" not in rendered_denied
        and "private-launchplane.example.invalid" not in rendered_denied,
        "Write-action denied summary must not render provider error message details",
    )

    unauthorized_error = urllib.error.HTTPError(
        "https://launchplane.example.invalid/v1/example",
        401,
        "Unauthorized",
        hdrs=Message(),
        fp=io.BytesIO(b"{}"),
    )
    unauthorized_payload = module.summarize_http_error(
        operation="product-config-preflight",
        request={"product": "example-product", "context": "example-testing"},
        exc=unauthorized_error,
    )
    require(
        unauthorized_payload["status"] == "unauthorized"
        and unauthorized_payload["summary"]["error_code"] == "unauthorized",
        "Write-action 401 without error body must summarize as unauthorized, not provider unavailable",
    )


def test_infra_ops_private_context_command_detects_docs_pointer() -> None:
    argv = command_argv("infra-ops", "infra-ops-private-context")
    with tempfile.TemporaryDirectory() as tmp:
        context_path = Path(tmp) / "local-context.toml"
        context_path.write_text('[docs]\nlocal_infra = "private/ops"\n', encoding="utf-8")
        command = [*argv, "--local-context", str(context_path)]
        result = run_probe(
            command,
            cwd=ROOT / "infra-ops",
        )

    require(
        result.returncode == 0 and result.stdout.strip() == "configured",
        "Infra ops private context command must detect [docs].local_infra without printing its value",
    )
    require(
        "private/ops" not in result.stdout + result.stderr,
        "Infra ops private context command must not print the private path value",
    )


def test_infra_ops_private_context_command_reports_missing() -> None:
    argv = command_argv("infra-ops", "infra-ops-private-context")
    with tempfile.TemporaryDirectory() as tmp:
        context_path = Path(tmp) / "local-context.toml"
        context_path.write_text('[docs]\nother = "private/ops"\n', encoding="utf-8")
        command = [*argv, "--local-context", str(context_path)]
        result = run_probe(
            command,
            cwd=ROOT / "infra-ops",
        )

    require(
        result.returncode != 0 and result.stdout.strip() == "missing",
        "Infra ops private context command must report missing when [docs].local_infra is absent",
    )


def main() -> None:
    tests = [
        test_launchplane_write_action_helper_contract,
        test_infra_ops_private_context_command_detects_docs_pointer,
        test_infra_ops_private_context_command_reports_missing,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"not ok {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
