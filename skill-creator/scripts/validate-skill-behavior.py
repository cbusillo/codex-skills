#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Small behavior-oriented checks for skill instructions.

These checks complement structural validation by guarding high-impact wording
that shapes when skills fire and what agents say when optional context is
unavailable.
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

import yaml


ROOT = Path(__file__).resolve().parents[2]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def command_argv(skill_name: str, command_name: str) -> list[str]:
    text = (ROOT / skill_name / "SKILL.md").read_text()
    marker = f"  - name: {command_name}"
    start = text.find(marker)
    require(start >= 0, f"{skill_name} must define {command_name}")
    block_end = text.find("\n  - name:", start + len(marker))
    if block_end < 0:
        block_end = text.find("\n---", start + len(marker))
    block = text[start:block_end]
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("example_argv:"):
            raw_json = stripped.split(":", 1)[1].strip()
            argv = json.loads(raw_json)
            require(isinstance(argv, list), f"{command_name} must define example_argv")
            return [str(token) for token in argv]
    raise AssertionError(f"{command_name} must define single-line example_argv")


def skill_frontmatter(skill_name: str) -> dict[str, Any]:
    text = (ROOT / skill_name / "SKILL.md").read_text()
    end = text.find("\n---", 4)
    require(text.startswith("---\n") and end >= 0, f"{skill_name} must have frontmatter")
    data = yaml.safe_load(text[4:end])
    require(isinstance(data, dict), f"{skill_name} frontmatter must be a mapping")
    return data


def command_policy_prefixes(skill_name: str) -> set[tuple[str, ...]]:
    frontmatter = skill_frontmatter(skill_name)
    policies = frontmatter.get("policy", {}).get("command_policies", [])
    prefixes: set[tuple[str, ...]] = set()
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        match = policy.get("match")
        if not isinstance(match, dict):
            continue
        prefix = match.get("argv_prefix")
        if isinstance(prefix, list):
            prefixes.add(tuple(str(token) for token in prefix))
    return prefixes


def command_policy_by_id(skill_name: str, policy_id: str) -> dict[str, Any]:
    frontmatter = skill_frontmatter(skill_name)
    policies = frontmatter.get("policy", {}).get("command_policies", [])
    require(isinstance(policies, list), f"{skill_name} command policies must be a list")
    for policy in policies:
        if isinstance(policy, dict) and policy.get("id") == policy_id:
            return policy
    raise AssertionError(f"{skill_name} must define command policy {policy_id}")


def test_chronicle_stays_quiet_when_unavailable() -> None:
    text = (ROOT / "chronicle" / "SKILL.md").read_text()
    lower = text.lower()
    normalized = " ".join(lower.split())

    require(
        "do not use it for ordinary repo, github, filesystem, or memory-context questions" in normalized,
        "Chronicle must not trigger for ordinary repo/GitHub/filesystem/memory questions",
    )
    require(
        "do not mention chronicle status unless the user explicitly asked" in normalized,
        "Chronicle unavailable status should stay quiet unless the user asked for it",
    )
    require(
        "this skill must be used whenever you need to resolve ambiguity" not in normalized,
        "Chronicle must not regain broad mandatory ambiguity-trigger wording",
    )
    require(
        "confirm the executable is `codex_chronicle`" in normalized
        and "do not trust sandbox-limited process checks" in normalized
        and "cannot be verified from the host" in normalized,
        "Chronicle must require host-visible process identity verification before use",
    )


def test_launchplane_product_config_uses_operator_api_first() -> None:
    text = (ROOT / "launchplane" / "SKILL.md").read_text()
    context_contract = (
        ROOT / "launchplane" / "references" / "context-helper-contract.md"
    ).read_text()
    operator_contract = (
        ROOT / "launchplane" / "references" / "operator-contract.md"
    ).read_text()
    public_safety = (ROOT / "launchplane" / "references" / "public-safety.md").read_text()
    lower = text.lower()
    normalized = " ".join(lower.split())
    normalized_context = " ".join(context_contract.lower().split())
    normalized_operator = " ".join(operator_contract.lower().split())
    normalized_public = " ".join(public_safety.lower().split())

    runtime_authority_terms = (
        "product",
        "tenant",
        "repository",
        "branch",
        "domain",
        "lane",
        "provider-target",
        "runtime-environment",
        "authz",
        "operator",
    )

    require(
        "runtime authority boundary" in normalized,
        "Launchplane skill must put the runtime authority boundary near the top",
    )
    require(
        "checked-in files are not runtime authority" in normalized,
        "Launchplane skill must reject checked-in files as runtime authority",
    )
    require(
        all(term in normalized for term in runtime_authority_terms),
        "Launchplane skill must name non-secret topology authority examples",
    )
    require(
        "non-secret topology can still steer production behavior" in normalized,
        "Launchplane skill must distinguish runtime authority from secrets only",
    )
    require(
        "never use them as evidence of the current live value" in normalized,
        "Launchplane skill must forbid treating checked-in hints as live values",
    )
    require(
        "service/operator state wins" in normalized,
        "Launchplane skill must prefer service/operator state over stale repo metadata",
    )
    require(
        "checked-in config, workflow defaults, checked-in examples, and archived workstation files are not authoritative"
        in normalized_operator,
        "Launchplane operator contract must reject checked-in/workflow/workstation authority",
    )
    require(
        "fail closed and ask for the service/operator source instead of inferring it from repo-local files"
        in normalized_operator,
        "Launchplane operator contract must fail closed instead of inferring live topology",
    )
    require(
        "checked-in repo metadata, workflow defaults, examples, and archived workstation files"
        in normalized_context,
        "Launchplane context contract must keep checked-in metadata as routing context only",
    )
    require(
        "they are not authoritative for live product" in normalized_context,
        "Launchplane context contract must reject checked-in metadata as live authority",
    )
    require(
        "this is not only a secret-handling rule" in normalized_public,
        "Launchplane public-safety guidance must cover non-secret runtime authority",
    )

    require(
        "use the service api path from the operator contract first" in normalized,
        "Launchplane product-config work should start with the service API path",
    )
    require(
        "post /v1/product-config/apply" in normalized,
        "Launchplane operator guidance must name the product-config service route",
    )
    require(
        "prefer signed-in, scoped operator sessions" in normalized,
        "Launchplane operator guidance must prefer signed-in scoped operator sessions",
    )
    require(
        "source terminal/local operator credentials only through the operator contract" in normalized,
        "Launchplane operator guidance must keep terminal credentials contract-bound",
    )
    require(
        "missing private config means the write-capable path is unavailable and must fail closed" in normalized,
        "Launchplane operator guidance must fail closed when private config is missing",
    )
    require(
        "do not use `.github/github.override.json` for launchplane credentials" in normalized,
        "Launchplane operator guidance must not store credentials in GitHub overrides",
    )
    require(
        "post /v1/work-graph/merge-train/controller/run-once" in normalized,
        "Launchplane guidance must name the merge-train controller route",
    )
    require(
        "phase-specific merge-train endpoints as detail or recovery surfaces" in normalized,
        "Launchplane merge-train guidance must not make phase endpoints the default path",
    )
    require(
        "do not hardcode repositories, labels, tokens" in normalized,
        "Launchplane guidance must forbid hardcoded merge-train/operator config",
    )
    require(
        "do not assume a global `launchplane` binary exists" in normalized,
        "Launchplane guidance must not make the global CLI the first-shot path",
    )
    require(
        "launchplane host-only cli helpers" in normalized,
        "Launchplane CLI guidance should be quarantined as host-only",
    )
    require(
        "explicitly on the launchplane host via ssh" in normalized,
        "Launchplane CLI guidance should require the host or a concrete command",
    )


def test_launchplane_operator_config_stays_private_and_optional() -> None:
    contract = (ROOT / "launchplane" / "references" / "operator-contract.md").read_text()
    normalized = " ".join(contract.lower().split())

    require(
        "terminal/operator execution is optional private configuration" in normalized,
        "Launchplane operator config must remain optional private configuration",
    )
    require(
        "references/launchplane-operator.local.example.json" in normalized,
        "Launchplane operator contract must point to the fake local config example",
    )
    require(
        "real token values stay in the operator's private environment or secret manager" in normalized,
        "Launchplane operator contract must keep real tokens out of committed config",
    )
    require(
        "missing private config is a normal unavailable state" in normalized,
        "Launchplane operator contract must treat missing private config as unavailable",
    )
    require(
        "explicit write actions must fail closed" in normalized,
        "Launchplane operator contract must fail closed for explicit writes",
    )
    require(
        "do not use `.github/github.override.json` for secrets" in normalized,
        "Launchplane operator contract must forbid storing secrets in GitHub overrides",
    )

    example = json.loads(
        (ROOT / "launchplane" / "references" / "launchplane-operator.local.example.json").read_text()
    )
    require(
        example["service_url"] == "https://launchplane.example.invalid",
        "Launchplane operator example must use a fake public-safe service URL",
    )
    require(
        example["operator_token_env"] == "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
        "Launchplane operator example must name an env var instead of storing a token",
    )
    require(
        not any(str(value).startswith(("ghp_", "github_pat_", "sk-")) for value in example.values()),
        "Launchplane operator example must not contain token-like placeholder values",
    )


def test_launchplane_write_action_helper_contract() -> None:
    skill_text = (ROOT / "launchplane" / "SKILL.md").read_text()
    contract = (
        ROOT / "launchplane" / "references" / "write-action-helper-contract.md"
    ).read_text()
    helper = ROOT / "launchplane" / "scripts" / "launchplane-write-action.py"
    helper_text = helper.read_text()
    normalized_skill = " ".join(skill_text.lower().split())
    normalized_contract = " ".join(contract.lower().split())
    normalized_helper = " ".join(helper_text.lower().split())

    require(
        "scripts/launchplane-write-action.py" in normalized_skill,
        "Launchplane skill must point agents at the write-action helper",
    )
    require(
        "post /v1/agent/write-intents/evaluate" in normalized_skill,
        "Launchplane skill must name the product-config preflight route",
    )
    require(
        "never from chat, cli plaintext secret args, or committed examples" in normalized_skill,
        "Launchplane skill must forbid plaintext secret helper input surfaces",
    )
    require(
        "the payload file is explicit private operator input" in normalized_contract,
        "Write-action contract must identify payload files as private operator input",
    )
    require(
        "must live outside the active repository or worktree" in normalized_contract,
        "Write-action contract must keep payload files outside the repo/worktree",
    )
    require(
        "unsupported runtime-authority shapes must also fail closed" in normalized_contract,
        "Write-action contract must fail closed for checked-in runtime authority shapes",
    )
    require(
        "explicit write actions fail closed" in normalized_contract,
        "Write-action contract must fail closed for explicit writes",
    )
    require(
        "product-config-preflight" in normalized_contract,
        "Write-action contract must document product-config preflight",
    )
    require(
        "product-config-dry-run" in normalized_contract
        and "product-config-apply" in normalized_contract,
        "Write-action contract must document product-config dry-run/apply helper entrypoints",
    )
    require(
        "merge-train-controller-run-once" in normalized_contract,
        "Write-action contract must document merge-train controller helper entrypoint",
    )
    require(
        "--idempotency-key" in normalized_contract,
        "Write-action contract must require idempotency for mutating calls",
    )
    require(
        "does not accept plaintext secrets as cli arguments" in normalized_contract,
        "Write-action contract must forbid plaintext secret CLI arguments",
    )
    require(
        "/v1/agent/write-intents/evaluate" in normalized_helper,
        "Write-action helper must call the agent write-intent preflight route",
    )
    require(
        "/v1/work-graph/merge-train/controller/run-once" in normalized_helper,
        "Write-action helper must call the merge-train controller route",
    )
    require(
        "stdin_payload_unsupported" in normalized_helper,
        "Write-action helper must refuse stdin payload transport",
    )
    require(
        "repo_local_payload_unsupported" in normalized_helper,
        "Write-action helper must refuse repo-local payload files",
    )
    require(
        "idempotency_key_required" in normalized_helper,
        "Write-action helper must require idempotency keys for mutating calls",
    )
    require(
        "ambiguous_service_url" in normalized_helper
        and "missing_service_url" in normalized_helper
        and "missing_operator_token" in normalized_helper,
        "Write-action helper must distinguish missing URL, missing token, and ambiguous public URL states",
    )
    require(
        "launchplane_public_url" in normalized_helper,
        "Write-action helper must detect LAUNCHPLANE_PUBLIC_URL as a diagnostic near-miss",
    )
    require(
        "validate_service_url" in normalized_helper and "safe_urlopen" in normalized_helper,
        "Write-action helper must use shared endpoint validation and redirect-safe transport",
    )
    require(
        "_project_success_output" in normalized_helper and "unsafe_response_shape" in normalized_helper,
        "Write-action helper must use operation-specific output projections and fail closed",
    )
    require(
        "non-loopback destinations must use https" in normalized_contract
        and "plain http is accepted only for explicit loopback hosts" in normalized_contract,
        "Write-action contract must document HTTPS and loopback-only HTTP endpoint policy",
    )
    require(
        "redirects are followed only when they stay on the same" in normalized_contract,
        "Write-action contract must document same-origin redirect credential safety",
    )
    require(
        "operation-specific projections instead of generic provider dictionary pass-through" in normalized_contract,
        "Write-action contract must document operation-specific public-safe projections",
    )

    no_context = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--config",
            str(ROOT / ".missing-launchplane-operator-config.json"),
            "merge-train-controller-run-once",
            "--repo",
            "example/repo",
            "--base-branch",
            "main",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("LAUNCHPLANE_")
        },
    )
    require(no_context.returncode == 2, "Write-action helper must fail closed without config")
    payload = json.loads(no_context.stdout)
    require(payload["status"] == "no_context", "Write-action helper must emit no_context")
    require(
        "missing_operator_config" in json.dumps(payload),
        "Write-action helper must explain missing operator config compactly",
    )

    missing_url = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--env-config",
            str(ROOT / ".missing-launchplane-operator.env"),
            "merge-train-controller-run-once",
            "--repo",
            "example/repo",
            "--base-branch",
            "main",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith("LAUNCHPLANE_")
            },
            "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "secret-token-never-render",
        },
    )
    require(missing_url.returncode == 2, "Write-action helper must fail closed without service URL")
    missing_url_payload = json.loads(missing_url.stdout)
    require(
        missing_url_payload["summary"]["configuration_state"] == "missing_service_url",
        "Write-action helper must distinguish a missing service URL from a missing credential",
    )
    require(
        "local operator token material is present"
        in str(missing_url_payload["summary"].get("recommendation", "")).lower(),
        "Write-action helper must explain missing_service_url as local operator routing setup",
    )

    missing_token = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--env-config",
            str(ROOT / ".missing-launchplane-operator.env"),
            "--url",
            "https://launchplane.example.invalid",
            "merge-train-controller-run-once",
            "--repo",
            "example/repo",
            "--base-branch",
            "main",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("LAUNCHPLANE_")
        },
    )
    require(missing_token.returncode == 2, "Write-action helper must fail closed without token")
    missing_token_payload = json.loads(missing_token.stdout)
    require(
        missing_token_payload["summary"]["configuration_state"] == "missing_operator_token",
        "Write-action helper must distinguish a missing token from missing operator config",
    )

    public_url_hint = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--env-config",
            str(ROOT / ".missing-launchplane-operator.env"),
            "operator-config-diagnostic",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith("LAUNCHPLANE_")
            },
            "LAUNCHPLANE_PUBLIC_URL": "https://public-launchplane.example.invalid",
            "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "secret-token-never-render",
        },
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

    repo_payload = ROOT / ".tmp-launchplane-repo-payload.json"
    try:
        repo_payload.write_text('{"reason":"example"}\n', encoding="utf-8")
        repo_local = subprocess.run(
            [
                sys.executable,
                str(helper),
                "--config",
                str(ROOT / ".missing-launchplane-operator-config.json"),
                "product-config-dry-run",
                "--payload-file",
                str(repo_payload),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("LAUNCHPLANE_")
            },
        )
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
        external_private = subprocess.run(
            [
                sys.executable,
                str(helper),
                "--config",
                str(ROOT / ".missing-launchplane-operator-config.json"),
                "product-config-dry-run",
                "--payload-file",
                str(external_payload),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("LAUNCHPLANE_")
            },
        )
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
        repo_symlink = ROOT / ".tmp-launchplane-repo-payload-link.json"
        try:
            repo_symlink.symlink_to(external_payload)
            symlink_local = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "--config",
                    str(ROOT / ".missing-launchplane-operator-config.json"),
                    "product-config-dry-run",
                    "--payload-file",
                    str(repo_symlink),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("LAUNCHPLANE_")
                },
            )
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
        diagnostic = subprocess.run(
            [
                sys.executable,
                str(helper),
                "--env-config",
                str(env_path),
                "operator-config-diagnostic",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("LAUNCHPLANE_")
            },
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
        precedence = subprocess.run(
            [
                sys.executable,
                str(helper),
                "--config",
                str(json_path),
                "--env-config",
                str(env_path),
                "operator-config-diagnostic",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("LAUNCHPLANE_")
                },
                "LAUNCHPLANE_OPERATOR_URL": "https://process-env.example.invalid",
            },
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
    except ValueError as exc:
        require(str(exc) == "unsafe_response_shape", "Unsafe provider success shapes must fail closed")
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
    require(
        "authz reconciliation" in denied_payload["summary"]["recommendation"],
        "Write-action denied recommendation must route to authz reconciliation instead of credential discovery",
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


def test_stale_injected_override_paths_are_nonfatal() -> None:
    validator_path = ROOT / "skill-creator" / "scripts" / "validate-skill-repo.py"
    injected = str(ROOT / ".system" / "plan" / "SKILL.md")
    proc = subprocess.run(
        ["uv", "run", str(validator_path)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "CODEX_SKILLS_INJECTED_PATHS": injected},
    )
    require(
        proc.returncode == 0,
        "Skill repo validation must not fail only because injected runtime metadata names a stale .system override path",
    )

    (ROOT / ".local").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / ".local") as tmp:
        tmp_root = Path(tmp)
        for name in ("plan", "plugin-creator", "skill-creator"):
            (tmp_root / name).mkdir()
        collect_missing = subprocess.run(
            [
                "uv",
                "run",
                "--with",
                "PyYAML>=6.0.0",
                "python3",
                "-c",
                (
                    "import importlib.util, json, pathlib; "
                    f"path = pathlib.Path({str(validator_path)!r}); "
                    "spec = importlib.util.spec_from_file_location('validator', path); "
                    "module = importlib.util.module_from_spec(spec); "
                    "spec.loader.exec_module(module); "
                    f"root = pathlib.Path({str(tmp_root)!r}); "
                    "print(json.dumps(module.validate_system_override_paths([root / 'plan', root / 'plugin-creator', root / 'skill-creator'])))"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    require(collect_missing.returncode == 0, "Skill repo validator missing-override probe must run")
    missing = json.loads(collect_missing.stdout)
    require(
        len(missing) == 3,
        "Skill repo validation must report every missing override skill, not only the first",
    )


def test_github_plan_sweeps_stale_related_issues() -> None:
    plan_text = (ROOT / "github-plan" / "SKILL.md").read_text().lower()
    github_text = (ROOT / "github" / "SKILL.md").read_text().lower()
    normalized_plan = " ".join(plan_text.split())
    normalized_github = " ".join(github_text.split())

    require(
        "stale github planning state is a regression source" in normalized_plan,
        "GitHub planning guidance must name stale issues as a regression source",
    )
    require(
        "update every related issue" in normalized_plan,
        "GitHub planning closeout must update all related issues that changed",
    )
    require(
        "stale, duplicate, related, and pr-linked issues were swept" in normalized_plan,
        "GitHub plan closeout checklist must include stale/duplicate/related/PR-linked sweep",
    )
    require(
        "use `github-plan` to sweep stale/duplicate/related planning issues" in normalized_github,
        "GitHub execution closeout must delegate related issue sweep to github-plan",
    )


def test_github_plan_prefers_plan_close_for_completed_plans() -> None:
    plan_text = (ROOT / "github-plan" / "SKILL.md").read_text().lower()
    github_text = (ROOT / "github" / "SKILL.md").read_text().lower()
    normalized_plan = " ".join(plan_text.split())
    normalized_github = " ".join(github_text.split())
    close_argv = command_argv("github-plan", "github-plan-close")
    close_argv_text = " ".join(close_argv)
    raw_issue_close_policy = command_policy_by_id("github", "prefer-gh-issue-close-helper")
    raw_issue_close_preferred = raw_issue_close_policy.get("preferred", [])
    require(
        isinstance(raw_issue_close_preferred, list),
        "github raw issue close policy preferred entries must be a list",
    )
    raw_issue_close_preferred_text = " ".join(
        " ".join(str(token) for token in entry.get("example_argv", []))
        for entry in raw_issue_close_preferred
        if isinstance(entry, dict)
    ).lower()

    require(
        close_argv[:3] == ["uv", "run", "$CODE_HOME/skills/github/scripts/gh-plan.py"],
        "github-plan close command metadata must invoke gh-plan.py through uv",
    )
    require(
        "close" in close_argv and "--comment-file" in close_argv,
        "github-plan close command metadata must expose close --comment-file",
    )
    require(
        "gh-issue" not in close_argv_text,
        "github-plan close command metadata must not route plan closure through gh-issue",
    )
    require(
        "for completed durable plan issues, use `gh-plan.py close`" in normalized_plan,
        "github-plan must prefer gh-plan close for completed plan issues",
    )
    require(
        "owns `plan:done` labels, stale `plan:active` cleanup, and project focus updates" in normalized_plan,
        "github-plan must explain why generic issue close is insufficient for plans",
    )
    require(
        "generic `github/scripts/gh-issue close` helper is for non-plan issues" in normalized_plan,
        "github-plan must reserve generic issue close for non-plan issues",
    )
    require(
        "closing a durable plan with the generic issue helper can leave planning labels or project fields stale" in normalized_plan,
        "github-plan must warn about stale labels or Project fields after generic close",
    )
    require(
        "use `gh-plan.py close --comment-file` for durable plan issues" in normalized_plan,
        "github-plan related issue sweep must route plan issue closure through gh-plan close",
    )
    require(
        "if the target is a completed durable plan issue, switch to `github-plan`" in normalized_github
        and "uv run $code_home/skills/github/scripts/gh-plan.py close 123 --comment-file comment.md" in normalized_github,
        "github raw issue close policy must mention the durable-plan exception without owning it",
    )
    require(
        "closes ordinary non-plan issues by reading an optional close comment from stdin" in normalized_github,
        "github raw issue close policy must keep gh-issue close for non-plan issues",
    )
    require(
        "github/scripts/gh-issue close" in raw_issue_close_preferred_text,
        "github raw issue close policy must prefer gh-issue for ordinary closes",
    )
    require(
        "gh-plan.py" not in raw_issue_close_preferred_text,
        "github raw issue close policy must not list gh-plan as an unconditional preferred helper",
    )


def test_github_and_github_plan_command_boundaries_are_partitioned() -> None:
    github_prefixes = command_policy_prefixes("github")
    plan_prefixes = command_policy_prefixes("github-plan")

    require(
        ("gh", "issue", "list") in plan_prefixes,
        "github-plan must own gh issue list planning indexes",
    )
    require(
        ("gh", "search", "issues") in plan_prefixes,
        "github-plan must own gh search issues planning discovery",
    )
    require(("gh", "project") in plan_prefixes, "github-plan must own gh project operations")
    require(
        not github_prefixes & plan_prefixes,
        f"github and github-plan command policy prefixes must not overlap: {github_prefixes & plan_prefixes}",
    )
    for execution_prefix in (
        ("gh", "pr", "create"),
        ("gh", "pr", "merge"),
        ("gh", "issue", "create"),
        ("gh", "issue", "edit"),
        ("gh", "issue", "close"),
    ):
        require(execution_prefix in github_prefixes, f"github must own {execution_prefix}")


def test_github_cross_repo_pr_create_is_explicit() -> None:
    github_text = (ROOT / "github" / "SKILL.md").read_text().lower()
    normalized = " ".join(github_text.split())

    require(
        "when creating a pr for a repository other than the current working directory" in normalized,
        "GitHub skill must warn about cross-repo PR creation context",
    )
    require(
        "pass both `--repo owner/repo` and an explicit `--head` branch" in normalized,
        "GitHub skill must require --repo plus explicit --head for cross-repo PR create",
    )


def test_github_merges_land_through_prs() -> None:
    github_text = (ROOT / "github" / "SKILL.md").read_text().lower()
    normalized = " ".join(github_text.split())

    require(
        "merging implementation work means merging a pull request through github" in normalized,
        "GitHub skill must define implementation merges as PR merges",
    )
    require(
        "do not locally merge a task branch into a protected, default, shared, release, or production branch" in normalized,
        "GitHub skill must forbid local task-branch merges into protected branches",
    )
    require(
        "local branch integration is only appropriate for explicit local synchronization or stack maintenance" in normalized,
        "GitHub skill must reserve local merges for explicit sync/stack maintenance",
    )
    require(
        "accidentally merged into a protected/default/shared branch locally" in normalized,
        "GitHub skill must include recovery guidance for accidental local protected-branch merges",
    )
    require(
        "do not push the accidental local protected-branch merge" in normalized,
        "GitHub skill must forbid pushing accidental local protected-branch merges",
    )


def test_runtime_checkout_reconciliation_is_safe_and_delegated() -> None:
    github_text = " ".join((ROOT / "github" / "SKILL.md").read_text().lower().split())
    babysit_text = " ".join((ROOT / "babysit-pr" / "SKILL.md").read_text().lower().split())
    launchplane_text = " ".join((ROOT / "launchplane" / "SKILL.md").read_text().lower().split())
    closeout_text = " ".join((ROOT / "work-closeout" / "SKILL.md").read_text().lower().split())
    inspection_text = " ".join((ROOT / "jetbrains-inspection" / "SKILL.md").read_text().lower().split())
    watcher_text = (ROOT / "babysit-pr" / "scripts" / "gh_pr_watch.py").read_text().lower()

    require(
        "keep the remote merge result separate from local runtime reconciliation" in github_text,
        "GitHub must preserve remote merge success independently of runtime reconciliation",
    )
    require(
        "must never cause the merge to be retried" in github_text,
        "Runtime reconciliation failure must not retry a confirmed merge",
    )
    require(
        "does not reconcile or mutate a local runtime checkout" in babysit_text,
        "babysit-pr must not mutate the runtime checkout",
    )
    require(
        "merge_commit_sha" in watcher_text and '"mergecommit"' in watcher_text,
        "babysit-pr must surface the final merge commit instead of only the PR head",
    )
    require(
        "do nothing for a merely closed, unmerged pr" in babysit_text,
        "babysit-pr must not reconcile closed-unmerged PRs",
    )
    require(
        "after the controller confirms a final landing commit" in launchplane_text,
        "Launchplane must delegate reconciliation only after final landing",
    )
    require(
        "idempotent closeout backstop" in closeout_text,
        "work-closeout must provide runtime reconciliation as a backstop",
    )
    require(
        "a missing or mismatched revision makes the installed-runtime claim `unknown`" in inspection_text,
        "JetBrains evidence must reject stale installed helper provenance",
    )


def test_repo_readiness_and_work_closeout_share_handoff_contract() -> None:
    readiness_text = (ROOT / "repo-readiness" / "SKILL.md").read_text().lower()
    closeout_text = (ROOT / "work-closeout" / "SKILL.md").read_text().lower()
    normalized_readiness = " ".join(readiness_text.split())
    normalized_closeout = " ".join(closeout_text.split())
    shared_schema = (
        "qualitygate", "docs", "metadatafreshness", "cleanup", "importantworkflows"
    )

    require(
        "readiness to closeout handoff" in normalized_readiness,
        "repo-readiness must define the closeout handoff surface",
    )
    require(
        "consuming readiness evidence" in normalized_closeout,
        "work-closeout must define how it consumes readiness evidence",
    )
    for key in shared_schema:
        require(
            key in normalized_readiness and key in normalized_closeout,
            f"repo-readiness and work-closeout must both name github.json field {key}",
        )
    require(
        "this handoff is evidence for `work-closeout`; it is not cleanup" in normalized_readiness,
        "repo-readiness must not claim cleanup ownership",
    )
    require(
        "do not treat closeout cleanup as proof that gates passed" in normalized_closeout,
        "work-closeout must not treat cleanup as readiness proof",
    )
    require(
        "if the readiness handoff is missing, stale, tied to a different commit/pr" in normalized_closeout,
        "work-closeout must reject stale or missing readiness handoffs",
    )
    require(
        "use `github-plan` for planning issue indexes, project state, blocker graphs, and planning graphql operations" in normalized_readiness,
        "repo-readiness must delegate planning GitHub surfaces to github-plan",
    )
    require(
        "this skill decides what evidence is needed, not which raw `gh` command to run" in normalized_readiness,
        "repo-readiness must separate readiness judgment from raw GitHub command choice",
    )
    require(
        "use `github-plan` for planning issue indexes, project state, blocker graphs, and planning issue closure" in normalized_closeout,
        "work-closeout must delegate planning GitHub surfaces to github-plan",
    )
    require(
        "the github skills own helper-backed github writes and planning mutations" in normalized_closeout,
        "work-closeout must separate closeout judgment from GitHub write mechanics",
    )


def test_safe_exit_requires_love_gate_closeout() -> None:
    readiness_text = (ROOT / "repo-readiness" / "SKILL.md").read_text().lower()
    closeout_text = (ROOT / "work-closeout" / "SKILL.md").read_text().lower()
    normalized_readiness = " ".join(readiness_text.split())
    normalized_closeout = " ".join(closeout_text.split())

    require(
        "must not end with a readiness-only final answer" in normalized_readiness
        and "final answer must come from `work-closeout`" in normalized_readiness,
        "repo-readiness must route safe-to-exit prompts to work-closeout for the final answer",
    )
    require(
        "the closeout answer owns the final safe-to-exit verdict and must include `love gate`" in normalized_readiness,
        "repo-readiness must preserve Love Gate in safe-to-exit handoff guidance",
    )
    require(
        "this skill owns the final safe-to-exit answer" in normalized_closeout
        and "must include both `love gate` and `safe to exit`" in normalized_closeout,
        "work-closeout must own final safe-to-exit output and require Love Gate",
    )
    require(
        "safe-to-exit and closeout final answers must include a love gate section" in normalized_closeout
        and "explicit `love:` and `do not love:` entries" in normalized_closeout,
        "work-closeout frontmatter must expose the required Love Gate output shape",
    )
    require(
        "do not reduce love gate to `passed`, `ready`, or a generic approval sentence" in normalized_closeout,
        "work-closeout must prevent pass/fail-only Love Gate summaries",
    )
    require(
        "any final answer to a safe-to-exit, wrap-up, closeout, pause, or handoff prompt" in normalized_closeout
        and "is incomplete unless it includes a `love gate` section" in normalized_closeout,
        "work-closeout must make Love Gate mandatory for closeout-style final answers",
    )
    require(
        "required love gate" in normalized_closeout
        and "always include a brief \"love gate\" section" in normalized_closeout,
        "work-closeout must make the Love Gate visibly required",
    )
    require(
        "must include two explicit labels" in normalized_closeout
        and "`love:`" in normalized_closeout
        and "`do not love:`" in normalized_closeout
        and "nothing material" in normalized_closeout,
        "work-closeout Love Gate must include explicit Love and Do not love entries",
    )


def test_launchplane_delegates_github_surfaces() -> None:
    launchplane = (ROOT / "launchplane" / "SKILL.md").read_text().lower()
    normalized = " ".join(launchplane.split())

    require(
        "when launchplane work turns into github issue, pr, actions, review, comment, commit, or push work" in normalized,
        "launchplane must identify GitHub lifecycle work as a delegated surface",
    )
    require(
        "delegate that surface to `github` or `github-plan` before running commands" in normalized,
        "launchplane must delegate GitHub command ownership to GitHub skills",
    )
    require(
        "launchplane owns runtime/operator authority; the github skills own helper-backed github identity" in normalized,
        "launchplane must separate runtime authority from GitHub identity policy",
    )


def test_code_readiness_requires_jetbrains_inspection_evidence() -> None:
    readiness_text = (ROOT / "repo-readiness" / "SKILL.md").read_text().lower()
    closeout_text = (ROOT / "work-closeout" / "SKILL.md").read_text().lower()
    github_text = (ROOT / "github" / "SKILL.md").read_text().lower()
    inspection_text = (ROOT / "jetbrains-inspection" / "SKILL.md").read_text().lower()
    normalized_readiness = " ".join(readiness_text.split())
    normalized_closeout = " ".join(closeout_text.split())
    normalized_github = " ".join(github_text.split())
    normalized_inspection = " ".join(inspection_text.split())

    require(
        "qualitygate.inspection" in normalized_readiness
        and "jetbrains evidence" in normalized_readiness
        and "explicit not-run reason" in normalized_readiness,
        "repo-readiness must require JetBrains evidence or a not-run reason for configured code gates",
    )
    require(
        "missing inspection evidence" in normalized_readiness
        and "configured code gate" in normalized_readiness
        and "not fully ready" in normalized_readiness,
        "repo-readiness must not call configured code changes fully ready without inspection evidence",
    )
    require(
        "ask the user before changing repo policy" in normalized_readiness
        and "safe inferred route" in normalized_readiness,
        "repo-readiness must distinguish blank/suspicious durable config from safe one-off inference",
    )
    require(
        "pr creation/update" in normalized_github
        and "ready-to-merge claims" in normalized_github
        and "merges must carry jetbrains evidence" in normalized_github,
        "github workflow must carry JetBrains evidence through PR and merge moments",
    )
    require(
        "use a safe one-off `changed_files` check" in normalized_github
        and "ask the user before changing durable config" in normalized_github,
        "github workflow must ask before trusting blank or suspicious inspection config as durable policy",
    )
    require(
        "missing jetbrains evidence" in normalized_closeout
        and "code changes" in normalized_closeout
        and "safe to exit: yes" in normalized_closeout,
        "work-closeout must block safe-to-exit yes when configured code inspection evidence is missing",
    )
    require(
        "blank or feels wrong" in normalized_closeout
        and "helper's safe inferred route with `changed_files` scope" in normalized_closeout,
        "work-closeout must preserve the ask-vs-one-off distinction for inspection config",
    )
    require(
        "do not silently turn that inference into durable repo policy" in normalized_inspection
        and "ask the user before changing policy" in normalized_inspection,
        "jetbrains-inspection must teach safe inference without inventing durable policy",
    )
    require(
        "docs-only" in normalized_readiness
        and "do not run full code gates" in normalized_readiness
        and "docs-only or non-code edits" in normalized_inspection,
        "JetBrains inspection guidance must preserve the docs-only/non-code exemption path",
    )


def test_work_closeout_requires_issue_aware_safe_exit() -> None:
    closeout_text = (ROOT / "work-closeout" / "SKILL.md").read_text().lower()
    normalized = " ".join(closeout_text.split())

    require(
        "name the owning durable surface" in normalized,
        "work-closeout must require explicit owning surface acknowledgement",
    )
    require(
        "the pr, issue, github plan, saved local plan, or explicit \"none\"" in normalized,
        "work-closeout must enumerate PR/issue/plan/local-plan/none owning surfaces",
    )
    require(
        "owning durable surface was named as closed/updated with evidence" in normalized,
        "safe-to-exit yes must require the owning surface to be current or absent",
    )
    require(
        "left open or parked with the current blocker and next action" in normalized,
        "work-closeout must require parked owning surfaces to hold blocker and next action",
    )
    require(
        "owning issue, pr, github plan, or saved local plan remains stale" in normalized,
        "safe-to-exit no must block on stale owning durable surfaces",
    )
    require(
        "resolved without a merged pr" in normalized and "evidence-backed comment" in normalized,
        "work-closeout must cover resolved issue-backed work without a merged PR",
    )
    require(
        "source-of-truth docs" in normalized and "compare that state with the owning issue/plan" in normalized,
        "work-closeout must reconcile docs/source-of-truth state with GitHub issue/plan state",
    )


def test_infra_ops_owns_live_infra_actions() -> None:
    infra_source = (ROOT / "infra-ops" / "SKILL.md").read_text()
    infra_text = infra_source.lower()
    docs_text = (ROOT / "docs-lookup" / "SKILL.md").read_text().lower()
    routing_source = (ROOT / "docs-lookup" / "references" / "routing.md").read_text()
    routing_text = routing_source.lower()
    infra_normalized = " ".join(infra_text.split())
    docs_normalized = " ".join(docs_text.split())
    routing_normalized = " ".join(routing_text.split())

    require(
        "read-only inventory, health checks, guarded pilot writes" in infra_normalized,
        "Infra ops must visibly trigger for inventory, health checks, and pilot writes",
    )
    require(
        "production-impacting infra changes" in infra_normalized,
        "Infra ops must visibly trigger for production-impacting infra changes",
    )
    require(
        "`$CODE_HOME/local-context.toml`" in infra_source
        and "`$CODEX_HOME/local-context.toml`" in infra_source
        and "`~/.code/local-context.toml`" in infra_text
        and "`[docs].local_infra`" in infra_text,
        "Infra ops must route through the local context docs pointer",
    )
    require(
        "do not guess from provider dashboards, browser sessions, shell history, or `.env` files"
        in infra_normalized,
        "Infra ops must fail closed instead of guessing from dashboards or .env files",
    )
    require(
        "use `docs-lookup` first only when the task is still about finding docs" in infra_normalized,
        "Infra ops must keep docs-lookup as discovery-only handoff",
    )
    require(
        "do not use this skill to perform infrastructure actions" in docs_normalized,
        "Docs lookup must explicitly reject infrastructure actions",
    )
    require(
        "switch to the owning operator skill after identifying the docs/access path"
        in docs_normalized,
        "Docs lookup must hand off after docs/access-path discovery",
    )
    require(
        "$CODE_HOME/local-context.toml" in routing_source
        and "$CODEX_HOME/local-context.toml" in routing_source
        and "~/.code/local-context.toml" in routing_text
        and "[docs].local_infra" in routing_text
        and "do not fall back" in routing_normalized
        and "`.env` files" in routing_normalized,
        "Docs routing must keep local infra discovery on local-context.toml without .env fallback",
    )
    require(
        "private dns" in docs_normalized
        and "cloudflare" in docs_normalized
        and "verification cname" in docs_normalized
        and "product repo `.env` files" in docs_normalized
        and "hand live record inspection or mutation to `infra-ops`" in docs_normalized,
        "Docs lookup must route product-repo DNS/Cloudflare discovery to local infra before mutation",
    )
    require(
        "private dns and cloudflare requests are local-infrastructure routes"
        in routing_normalized
        and "bing verification cname or txt record" in routing_normalized
        and "use `[docs].local_infra` to find the private dns/cloudflare authority"
        in routing_normalized
        and "use `infra-ops` for live record inspection, mutation, rollback, and verification"
        in routing_normalized,
        "Docs routing must include the product-repo DNS/Cloudflare regression path",
    )
    require(
        "for dns or cloudflare work" in infra_normalized
        and "read the private dns/cloudflare docs or helper usage" in infra_normalized
        and "token values, zone identifiers, account details, and rollback specifics"
        in infra_normalized
        and "redacted verification results" in infra_normalized,
        "Infra ops must own DNS/Cloudflare operations without public topology details",
    )


def test_infra_ops_private_context_command_detects_docs_pointer() -> None:
    argv = command_argv("infra-ops", "infra-ops-private-context")
    with tempfile.TemporaryDirectory() as tmp:
        context_path = Path(tmp) / "local-context.toml"
        context_path.write_text('[docs]\nlocal_infra = "private/ops"\n', encoding="utf-8")
        command = [*argv, "--local-context", str(context_path)]
        result = subprocess.run(
            command,
            cwd=ROOT / "infra-ops",
            capture_output=True,
            text=True,
            check=False,
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
        result = subprocess.run(
            command,
            cwd=ROOT / "infra-ops",
            capture_output=True,
            text=True,
            check=False,
        )

    require(
        result.returncode != 0 and result.stdout.strip() == "missing",
        "Infra ops private context command must report missing when [docs].local_infra is absent",
    )


def test_dns_cloudflare_routes_to_local_infra_context() -> None:
    docs_text = (ROOT / "docs-lookup" / "SKILL.md").read_text().lower()
    routing_text = (ROOT / "docs-lookup" / "references" / "routing.md").read_text().lower()
    docs_normalized = " ".join(docs_text.split())
    routing_normalized = " ".join(routing_text.split())

    require(
        "description:" in docs_text
        and "dns" in docs_text.split("---", 2)[1]
        and "cloudflare" in docs_text.split("---", 2)[1],
        "Docs lookup frontmatter must trigger for DNS/Cloudflare access discovery",
    )
    require(
        "private dns" in docs_normalized
        and "cloudflare" in docs_normalized
        and "verification cname" in docs_normalized
        and "product repo `.env` files" in docs_normalized
        and "common token locations" in docs_normalized
        and "hand live record inspection or mutation to `infra-ops`" in docs_normalized,
        "Docs lookup must route DNS/Cloudflare discovery to local infra before mutation or token search",
    )
    require(
        "private dns and cloudflare requests are local-infrastructure routes"
        in routing_normalized
        and "verification cname or txt record" in routing_normalized
        and "use `[docs].local_infra` to find the private dns/cloudflare authority"
        in routing_normalized
        and "do not start by scanning product repo `.env` files" in routing_normalized
        and "use `infra-ops` for live record inspection, mutation, rollback, and verification"
        in routing_normalized,
        "Docs routing must include the product-repo DNS/Cloudflare regression path",
    )


def test_openai_docs_latest_target_and_fallback_contract() -> None:
    source = (ROOT / "openai-docs" / "SKILL.md").read_text()
    normalized = " ".join(source.lower().split())

    require(
        "latest-model.md" in normalized
        and "node scripts/resolve-latest-model-info.js" in normalized,
        "OpenAI Docs must resolve latest-model metadata before dynamic migrations",
    )
    require(
        "non-2xx, empty, whitespace-only, or non-substantive" in normalized
        and "mcp fetch/search or official openai web search" in normalized
        and "bundled fallbacks" in normalized,
        "OpenAI Docs must route empty or failed remote guides through official search before bundled fallback",
    )
    require(
        "preserve explicit targets" in normalized
        and "historical docs, examples, eval baselines, comparison code" in normalized
        and "intentionally pinned fallbacks" in normalized,
        "OpenAI Docs must preserve explicit, historical, evaluation, and pinned model usage",
    )
    require(
        "keep optional capabilities" in normalized
        and "separate from the baseline migration" in normalized,
        "OpenAI Docs must keep optional GPT-5.6 capabilities out of baseline migrations",
    )
    require(
        "do not install or reconfigure mcp as a side effect of a docs lookup" in normalized,
        "OpenAI Docs must not mutate MCP configuration during ordinary docs lookup",
    )
    require(
        "run the install command yourself" not in normalized,
        "OpenAI Docs must not retain automatic MCP installation instructions",
    )


def test_gpt56_rollout_comparisons_preserve_pinned_baselines() -> None:
    skill_text = " ".join((ROOT / "rollout-friction" / "SKILL.md").read_text().lower().split())
    matrix_text = (ROOT / "rollout-friction" / "scripts" / "run_rollout_memory_long_context_matrix.py").read_text()

    require(
        "alongside the existing gpt-5.4 comparison" in skill_text
        and "keep new family variants opt-in" in skill_text,
        "Rollout guidance must add GPT-5.6 variants without replacing the GPT-5.4 baseline",
    )
    require(
        "preserve fake `gpt-5.1-codex` harness models" in skill_text,
        "Rollout guidance must preserve fake GPT-5.1-Codex protocol fixtures",
    )
    require(
        '"gpt-5.4=code-llm:gpt-5.4"' in matrix_text,
        "Rollout matrix must retain the pinned GPT-5.4 default comparison",
    )
    require(
        "gpt-5.6-sol=code-llm:gpt-5.6-sol" not in matrix_text,
        "GPT-5.6 cloud variants must remain opt-in instead of silently expanding the default matrix",
    )


def test_skill_creator_mentions_exec_harness_for_behavior_changes() -> None:
    creator_source = (ROOT / "skill-creator" / "SKILL.md").read_text()
    creator_text = creator_source.lower()
    normalized = " ".join(creator_text.split())

    require(
        "use the exec harness for behavior-sensitive skill changes when available" in normalized,
        "Skill creator guidance must call out exec harness validation",
    )
    require(
        "routing, command policy, safety boundaries, or github/repo workflow semantics" in normalized,
        "Skill creator guidance must identify behavior-sensitive skill changes",
    )
    require(
        "negative or ambiguity case when practical" in normalized,
        "Skill creator guidance must encourage ambiguity/negative harness cases",
    )
    frontmatter_end = creator_source.find("\n---", 4)
    require(frontmatter_end >= 0, "Skill creator must have closing frontmatter")
    body_lines = creator_source[frontmatter_end + 4 :].splitlines()
    require(
        len(body_lines) < 500,
        f"Skill creator body must stay under 500 lines (got {len(body_lines)})",
    )


def main() -> None:
    tests = [
        test_chronicle_stays_quiet_when_unavailable,
        test_launchplane_product_config_uses_operator_api_first,
        test_launchplane_operator_config_stays_private_and_optional,
        test_launchplane_write_action_helper_contract,
        test_stale_injected_override_paths_are_nonfatal,
        test_github_plan_sweeps_stale_related_issues,
        test_github_plan_prefers_plan_close_for_completed_plans,
        test_github_and_github_plan_command_boundaries_are_partitioned,
        test_github_cross_repo_pr_create_is_explicit,
        test_github_merges_land_through_prs,
        test_runtime_checkout_reconciliation_is_safe_and_delegated,
        test_repo_readiness_and_work_closeout_share_handoff_contract,
        test_safe_exit_requires_love_gate_closeout,
        test_launchplane_delegates_github_surfaces,
        test_code_readiness_requires_jetbrains_inspection_evidence,
        test_work_closeout_requires_issue_aware_safe_exit,
        test_infra_ops_owns_live_infra_actions,
        test_infra_ops_private_context_command_detects_docs_pointer,
        test_infra_ops_private_context_command_reports_missing,
        test_dns_cloudflare_routes_to_local_infra_context,
        test_openai_docs_latest_target_and_fallback_contract,
        test_gpt56_rollout_comparisons_preserve_pinned_baselines,
        test_skill_creator_mentions_exec_harness_for_behavior_changes,
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
