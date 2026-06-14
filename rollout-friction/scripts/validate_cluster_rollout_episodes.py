#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for cluster_rollout_episodes.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).with_name("cluster_rollout_episodes.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cluster_rollout_episodes", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load cluster_rollout_episodes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def episode(episode_id: str, cost: int, snippet: str, outcome: str = "unresolved") -> dict[str, object]:
    return {
        "schema_version": 1,
        "episode_id": episode_id,
        "source_file_id": f"file-{episode_id}",
        "start_line": 1,
        "end_line": 3,
        "primary_signal": "repeated_command_failure",
        "signals": ["repeated_command_failure", "shell_quoting_or_parse_error"],
        "severity": "medium",
        "category": "command-friction",
        "recommended_destination": "fix-script-or-helper",
        "outcome": outcome,
        "cost_score": cost,
        "likely_cause": "Commands or tools failed repeatedly.",
        "tag_counts": {"exit_code": 1},
        "hits": [
            {"signal": "repeated_command_failure", "line": 1, "snippet": snippet, "evidence_type": "structured_payload", "tags": ["exit_code"]},
            {"signal": "shell_quoting_or_parse_error", "line": 2, "snippet": "zsh: unmatched quote", "evidence_type": "structured_payload"},
        ],
    }


def test_clusters_by_signal_signature(module: ModuleType) -> None:
    payload = module.build_clusters(
        [episode("a", 10, "Process exited with code 1"), episode("b", 30, "Process exited with code 1")],
        top_clusters=10,
        max_steps=8,
        trusted_originals=False,
    )
    if payload["total_cluster_count"] != 1:
        raise AssertionError(f"expected one cluster, got {payload['total_cluster_count']}")
    cluster = payload["clusters"][0]
    if cluster["episode_count"] != 2 or cluster["max_cost_score"] != 30:
        raise AssertionError("cluster should aggregate episode count and representative cost")
    if cluster.get("tag_counts") != {"exit_code": 2}:
        raise AssertionError(f"cluster should aggregate failure tags: {cluster}")
    if "tags" not in payload["skeletons"][0]["steps"][0]:
        raise AssertionError("skeleton steps should preserve per-hit tags")


def test_skeleton_redacts_private_shapes(module: ModuleType) -> None:
    payload = module.build_clusters(
        [
            episode(
                "secret",
                40,
                "token=abc123 path /Users/example/private/file.py rel rollout-friction/scripts/foo.py "
                "dot ./local/file.py tmp /tmp/private.log tilde ~/.ssh/id_rsa email a@example.com",
            )
        ],
        top_clusters=10,
        max_steps=8,
        trusted_originals=False,
    )
    summary = payload["skeletons"][0]["steps"][0]["summary"]
    for forbidden in (
        "/Users/example",
        "rollout-friction/scripts",
        "./local/file.py",
        "/tmp/private",
        "~/.ssh",
        "a@example.com",
        "token=abc123",
    ):
        if forbidden in summary:
            raise AssertionError(f"skeleton summary leaked {forbidden!r}: {summary}")


def test_skeleton_preserves_markup_punctuation(module: ModuleType) -> None:
    summary = module.sanitize("<context>Review completed.</context> Next sentence.", trusted_originals=False)
    if "<path-redacted>" in summary:
        raise AssertionError(f"markup punctuation should not be redacted as a path: {summary}")
    if "completed.</context>" not in summary:
        raise AssertionError(f"expected markup sentence punctuation to survive: {summary}")


def test_step_kind_matches_success_as_word(module: ModuleType) -> None:
    if module.step_kind("repeated_command_failure", "unsuccessful attempt with exit code 1") == "resolution":
        raise AssertionError("unsuccessful should not be classified as a resolution")
    if module.step_kind("repeated_command_failure", "command succeeded after retry") != "resolution":
        raise AssertionError("succeeded should be classified as a resolution")
    if module.step_kind("repeated_command_failure", "pytest summary: 1 failed, 2 passed") == "resolution":
        raise AssertionError("mixed failure/success summaries should not be classified as resolution")


def test_step_kind_rejects_false_success_flags(module: ModuleType) -> None:
    for snippet in ('{"success": false, "error": "failed"}', "success=false exit_code=1"):
        if module.step_kind("repeated_command_failure", snippet) == "resolution":
            raise AssertionError(f"false success flag should not be classified as resolution: {snippet}")


def test_compacts_long_skeleton(module: ModuleType) -> None:
    steps = [{"kind": "signal", "summary": str(index)} for index in range(10)]
    compacted, elided_count = module.compact_steps(steps, max_steps=5)
    if len(compacted) != 5 or compacted[2]["kind"] != "elision" or elided_count != 6:
        raise AssertionError(f"expected elision in compacted skeleton, got {compacted}")
    compacted_one, elided_one = module.compact_steps(steps, max_steps=1)
    if len(compacted_one) != 1 or compacted_one[0]["kind"] != "elision" or elided_one != 10:
        raise AssertionError(f"max_steps=1 should stay capped with full elision, got {compacted_one}")
    compacted_two, elided_two = module.compact_steps(steps, max_steps=2)
    if len(compacted_two) != 2 or compacted_two[1]["kind"] != "elision" or elided_two != 9:
        raise AssertionError(f"max_steps=2 should stay capped with one real step and elision, got {compacted_two}")


def main() -> int:
    module = load_module()
    test_clusters_by_signal_signature(module)
    test_skeleton_redacts_private_shapes(module)
    test_skeleton_preserves_markup_punctuation(module)
    test_step_kind_matches_success_as_word(module)
    test_step_kind_rejects_false_success_flags(module)
    test_compacts_long_skeleton(module)
    print("ok validate-cluster-rollout-episodes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
