#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest>=8.0.0",
# ]
# ///
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


MODULE_PATH = Path(__file__).with_name("synthesize_work_brief.py")
MODULE_SPEC = importlib.util.spec_from_file_location("synthesize_work_brief", MODULE_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
synthesize_work_brief = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = synthesize_work_brief
MODULE_SPEC.loader.exec_module(synthesize_work_brief)


def evidence() -> dict[str, object]:
    return {
        "kind": "github_work_evidence",
        "source_notes": ["Workflow collection reached a cap; counts may be incomplete."],
        "buckets": {
            "ready_for_review": [
                {
                    "repo": "example-org/example-repo",
                    "number": 42,
                    "url": "https://github.com/example-org/example-repo/pull/42",
                    "title": "Add direct synthesis",
                }
            ]
        },
    }


def save_json(path: Path, payload: object) -> None:
    synthesize_work_brief.save_text(path, json.dumps(payload))


def read_text(path: Path) -> str:
    return synthesize_work_brief.read_text(path)


def args_for(tmp_path: Path, **overrides: Any) -> argparse.Namespace:
    evidence_path = tmp_path / "evidence.json"
    save_json(evidence_path, evidence())
    values: dict[str, Any] = {
        "evidence": evidence_path,
        "plan_context": [],
        "audience": "manager",
        "report_recipient": "Justin",
        "brief_output": tmp_path / "brief.md",
        "prompt_output": tmp_path / "prompt.txt",
        "llm_result_output": tmp_path / "llm.json",
        "role": "work_brief_writer",
        "local_llm_config": None,
        "endpoint": None,
        "base_url": None,
        "model": None,
        "temperature": None,
        "max_tokens": None,
        "timeout": None,
        "load_policy": None,
        "ttl": None,
        "context_length": None,
        "flash_attention": False,
        "warmup": False,
        "unload_after": False,
        "max_input_chars": synthesize_work_brief.DEFAULT_MAX_INPUT_CHARS,
        "allow_truncate": False,
        "no_verify": False,
        "json": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class FakeRunner:
    def __init__(self, *, verifier_returncode: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.verifier_returncode = verifier_returncode

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"command": command, "kwargs": kwargs})
        if "lm_studio_chat.py" in command[2]:
            payload = {
                "ok": True,
                "model": "qwen3-coder-64b",
                "served_model": "qwen3-coder-64b",
                "role": "work_brief_writer",
                "endpoint": {"id": "lmstudio-localhost", "locality": "localhost"},
                "prompt_chars": len(kwargs.get("input", "")),
                "max_tokens": 3500,
                "lifecycle": {"load_policy": "api_explicit"},
                "content": "example-org/example-repo#42 is ready. Source limitation: workflow counts may be incomplete.",
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        return subprocess.CompletedProcess(
            command,
            self.verifier_returncode,
            stdout="ok verify-work-brief\n" if self.verifier_returncode == 0 else "",
            stderr="unsupported issue/PR reference" if self.verifier_returncode else "",
        )


def test_synthesizes_with_exact_contract_and_verifies(tmp_path: Path) -> None:
    runner = FakeRunner()

    summary = synthesize_work_brief.synthesize(args_for(tmp_path), runner=runner)

    assert summary["ok"] is True
    assert summary["verified"] is True
    assert read_text(tmp_path / "brief.md").startswith("example-org/example-repo#42")
    prompt = read_text(tmp_path / "prompt.txt")
    assert "recipient: Justin" in prompt
    assert "Plan context: no plan signal was provided." in prompt
    assert "Source limitations to reflect in the brief:" in prompt
    assert "Workflow collection reached a cap" in prompt
    assert "example-org/example-repo" in prompt
    lm_call = runner.calls[0]
    command = lm_call["command"]
    assert command[:3] == ["uv", "run", str(synthesize_work_brief.LM_CHAT_PATH)]
    assert command[command.index("--role") + 1] == "work_brief_writer"
    assert command[command.index("--system") + 1] == read_text(synthesize_work_brief.CONTRACT_PATH)
    assert lm_call["kwargs"]["input"] == prompt
    verify_call = runner.calls[1]
    assert verify_call["command"][:3] == ["uv", "run", str(synthesize_work_brief.VERIFY_PATH)]


def test_passes_lifecycle_overrides_to_local_llm(tmp_path: Path) -> None:
    runner = FakeRunner()
    args = args_for(
        tmp_path,
        local_llm_config=tmp_path / "local-llm.yaml",
        endpoint="lmstudio-localhost",
        model="qwen3-coder-next",
        load_policy="jit_chat",
        ttl=60,
        context_length=65536,
        flash_attention=True,
        warmup=True,
        unload_after=True,
        max_tokens=2500,
        timeout=300,
        temperature=0.1,
    )

    synthesize_work_brief.synthesize(args, runner=runner)

    command = runner.calls[0]["command"]
    for flag in (
        "--config",
        "--endpoint",
        "--model",
        "--load-policy",
        "--ttl",
        "--context-length",
        "--max-tokens",
        "--timeout",
        "--temperature",
        "--flash-attention",
        "--warmup",
        "--unload-after",
    ):
        assert flag in command


def test_verifier_failure_returns_error_and_preserves_brief(tmp_path: Path) -> None:
    runner = FakeRunner(verifier_returncode=1)
    args = args_for(tmp_path)

    try:
        synthesize_work_brief.synthesize(args, runner=runner)
    except synthesize_work_brief.SynthesisError as exc:
        assert "brief verification failed" in str(exc)
        assert str(args.brief_output) in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected verifier failure")

    assert args.brief_output.exists()


def test_rejects_oversized_prompt_without_explicit_truncation(tmp_path: Path) -> None:
    args = args_for(tmp_path, max_input_chars=10)

    try:
        synthesize_work_brief.synthesize(args, runner=FakeRunner())
    except synthesize_work_brief.SynthesisError as exc:
        assert "exceeding --max-input-chars" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected oversized prompt failure")


def test_no_verify_skips_verifier(tmp_path: Path) -> None:
    runner = FakeRunner()

    summary = synthesize_work_brief.synthesize(args_for(tmp_path, no_verify=True), runner=runner)

    assert summary["verified"] is False
    assert len(runner.calls) == 1


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))
