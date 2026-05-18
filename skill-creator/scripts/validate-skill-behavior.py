#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
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
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


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


def test_launchplane_product_config_uses_operator_api_first() -> None:
    text = (ROOT / "launchplane" / "SKILL.md").read_text()
    lower = text.lower()
    normalized = " ".join(lower.split())

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
        "idempotency_key_required" in normalized_helper,
        "Write-action helper must require idempotency keys for mutating calls",
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
                "authorization": "Bearer ghp_example",
                "runtime": {"key": "NEXT_PUBLIC_EXAMPLE", "value": "must-not-render"},
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

    error_body = json.dumps(
        {
            "status": "rejected",
            "trace_id": "launchplane_req_denied",
            "error": {"code": "authorization_denied", "message": "Denied."},
        }
    ).encode()
    http_error = urllib.error.HTTPError(
        "https://launchplane.example.invalid/v1/example",
        403,
        "Forbidden",
        hdrs={},
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
        for name in ("plan", "skill-creator", "skill-installer"):
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
                    "print(json.dumps(module.validate_system_override_paths([root / 'plan', root / 'skill-creator', root / 'skill-installer'])))"
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


def main() -> None:
    tests = [
        test_chronicle_stays_quiet_when_unavailable,
        test_launchplane_product_config_uses_operator_api_first,
        test_launchplane_operator_config_stays_private_and_optional,
        test_launchplane_write_action_helper_contract,
        test_stale_injected_override_paths_are_nonfatal,
        test_github_plan_sweeps_stale_related_issues,
        test_github_cross_repo_pr_create_is_explicit,
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
