#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("local_code_agent", SCRIPT_DIR / "local_code_agent.py")
assert SPEC is not None and SPEC.loader is not None
local_code_agent = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = local_code_agent
SPEC.loader.exec_module(local_code_agent)


def args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "config": Path("missing.yaml"),
        "endpoint": None,
        "role": "rollout_scout",
        "model": None,
        "base_url": None,
        "code_bin": "code",
        "workdir": Path("/tmp/work"),
        "sandbox": "read-only",
        "max_seconds": 120,
        "context_window": None,
        "output_last_message": None,
        "json": False,
        "keep_code_home": None,
        "prompt": "hello",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_build_code_config_uses_chat_wire_api() -> None:
    config_text, summary = local_code_agent.build_code_config(args(model="qwen3-coder-64b"))

    assert 'model_provider = "lmstudio"' in config_text
    assert 'model = "qwen3-coder-64b"' in config_text
    assert 'base_url = "http://127.0.0.1:1234/v1"' in config_text
    assert 'wire_api = "chat"' in config_text
    assert summary["provider"] == "lmstudio"
    assert summary["model"] == "qwen3-coder-64b"


def test_build_code_config_uses_role_context_length() -> None:
    config_text, summary = local_code_agent.build_code_config(args(role="work_brief_writer"))

    assert "model_context_window = 131072" in config_text
    assert summary["context_window"] == 131072


def test_explicit_context_window_overrides_role() -> None:
    config_text, summary = local_code_agent.build_code_config(args(role="work_brief_writer", context_window=32768))

    assert "model_context_window = 32768" in config_text
    assert summary["context_window"] == 32768


def test_rejects_non_lm_studio_endpoint() -> None:
    try:
        local_code_agent.build_code_config(args(base_url="http://127.0.0.1:11434/v1", model="example-model"))
    except local_code_agent.LocalCodeAgentError as exc:
        assert "provider=lm_studio" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected non-LM Studio endpoint rejection")


def test_build_code_command() -> None:
    command = local_code_agent.build_code_command(
        args(json=True, output_last_message=Path("/tmp/last.txt"), max_seconds=45),
        "/usr/local/bin/code",
    )

    assert command == [
        "/usr/local/bin/code",
        "exec",
        "--skip-git-repo-check",
        "-C",
        "/tmp/work",
        "-s",
        "read-only",
        "--max-seconds",
        "45",
        "--json",
        "--output-last-message",
        "/tmp/last.txt",
        "-",
    ]


def test_redacted_command_hides_stdin_marker() -> None:
    assert local_code_agent.redacted_command(["code", "exec", "-"]) == ["code", "exec", "<prompt-stdin>"]


def main() -> int:
    tests = [
        test_build_code_config_uses_chat_wire_api,
        test_build_code_config_uses_role_context_length,
        test_explicit_context_window_overrides_role,
        test_rejects_non_lm_studio_endpoint,
        test_build_code_command,
        test_redacted_command_hides_stdin_marker,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
