#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for extract_rollout_memory.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from argparse import Namespace
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("extract_rollout_memory.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("extract_rollout_memory", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load extract_rollout_memory.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_trace(root: Path, records: list[dict[str, object]]) -> Path:
    path = root / "rollout-test.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def args(**overrides: object):
    module = load_module()
    defaults: dict[str, object] = {
        "max_bytes": 100_000,
        "context_events": 1,
        "max_record_chars": 500,
        "batch_chars": 10_000,
        "max_files": 100,
        "since": None,
        "until": None,
        "destination": None,
        "trusted_originals": True,
        "redact": False,
        "since_ts": None,
        "until_ts": None,
    }
    defaults.update(overrides)
    return Namespace(**defaults), module


def response_item(role: str, text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "timestamp": "2026-06-01T12:00:00Z",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        },
    }


def tool_output(text: str) -> dict[str, object]:
    return {
        "type": "function_call_output",
        "timestamp": "2026-06-01T12:00:01Z",
        "payload": {"stdout": text},
    }


def test_skips_session_meta_base_instructions() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "base_instructions": {
                            "text": "Available skills mention people and local LLM and manager routing."
                        }
                    },
                },
                response_item("user", "Remember that qwen3-coder-64b is preferred for rollout extraction."),
            ],
        )
        candidates = module.extract([trace], namespace)
    if len(candidates) != 1:
        raise AssertionError(f"expected one real candidate, got {len(candidates)}")
    if candidates[0].destination != "local-llm":
        raise AssertionError(f"expected local-llm candidate, got {candidates[0].destination}")


def test_context_window_preserves_neighboring_turns() -> None:
    namespace, module = args(context_events=1)
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                response_item("user", "We should make future scans destination-aware."),
                response_item("assistant", "I will remember that as a workflow preference."),
                response_item("user", "Never dump transient PR status into local memory."),
            ],
        )
        candidates = module.extract([trace], namespace)
    destinations = {candidate.destination for candidate in candidates}
    if "profile" not in destinations:
        raise AssertionError(f"expected profile destination, got {destinations}")
    never_candidate = next(candidate for candidate in candidates if "Never dump" in candidate.text)
    if len(never_candidate.context) < 2:
        raise AssertionError("context window should include neighboring turn")


def test_classifies_people_local_llm_and_friction() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                response_item("user", "Kyle/HonkHonk is a trusted collaborator, but ask before adding trust notes."),
                response_item("user", "qwen3-coder-64b is our preferred local LLM extraction model."),
                tool_output("Auto Review loop repeated and blocked the branch until confirm: git switch was used."),
            ],
        )
        candidates = module.extract([trace], namespace)
    destinations = [candidate.destination for candidate in candidates]
    for expected in ("people", "local-llm", "rollout-friction"):
        if expected not in destinations:
            raise AssertionError(f"missing {expected} from {destinations}")


def test_redact_mode_removes_paths_and_person_data_but_keeps_trusted_originals() -> None:
    redact_args, module = args(redact=True, trusted_originals=False)
    trusted_args, _module = args(redact=False, trusted_originals=True)
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                response_item(
                    "user",
                    (
                        "Remember local config path /Users/example/Developer/.code/local.env. "
                        "Jackie Example is the manager; GitHub handle @jackie-example, "
                        "Slack <@U12345>, Discord jackie-example#1234, email jackie@example.test. "
                        "Jackie is our organizer, TubeTester manages the video work, and handle TubeTester is bare. "
                        "Every Code and GitHub Actions should remain readable."
                    ),
                )
            ],
        )
        redacted = module.extract([trace], redact_args)[0].text
        redacted_source = module.extract([trace], redact_args)[0].source_file
        original_candidate = module.extract([trace], trusted_args)[0]
        original = original_candidate.text
    if "/Users/example" in redacted:
        raise AssertionError(f"redact mode leaked path: {redacted}")
    if "/" in redacted_source:
        raise AssertionError(f"redact mode leaked source path: {redacted_source}")
    for leaked in (
        "Jackie Example",
        "@jackie-example",
        "<@U12345>",
        "jackie-example#1234",
        "jackie@example.test",
        "TubeTester",
    ):
        if leaked in redacted:
            raise AssertionError(f"redact mode leaked person data {leaked!r}: {redacted}")
    for marker in ("<person-name-redacted>", "<person-handle-redacted>", "<person-email-redacted>"):
        if marker not in redacted:
            raise AssertionError(f"redact mode should include marker {marker}: {redacted}")
    if "Every Code" not in redacted:
        raise AssertionError(f"redact mode should preserve public product names: {redacted}")
    if "GitHub Actions" not in redacted:
        raise AssertionError(f"redact mode should preserve GitHub product names: {redacted}")
    if "/Users/example" not in original:
        raise AssertionError(f"trusted originals should preserve path: {original}")
    if (
        "Jackie Example" not in original
        or "@jackie-example" not in original
        or "jackie@example.test" not in original
        or "TubeTester" not in original
    ):
        raise AssertionError(f"trusted originals should preserve person data: {original}")
    if str(trace) != original_candidate.source_file:
        raise AssertionError(f"trusted originals should preserve source path: {original_candidate.source_file}")


def test_redact_source_metadata_is_independent_of_path_root() -> None:
    redact_args, module = args(redact=True, trusted_originals=False)
    trusted_args, _module = args(redact=False, trusted_originals=True)
    data = json.dumps(response_item("user", "Always prefer uv for Python commands.")).encode()
    for source in (
        "/Users/example/rollout.jsonl",
        "/tmp/example/rollout.jsonl",
        "/Volumes/EXAMPLE/Task Evidence/rollout.jsonl",
        "/mnt/example/rollout.jsonl",
    ):
        # Only the trace bytes are synthetic; extraction sees the explicit source
        # path without needing that directory or the host's temporary root.
        with patch.object(Path, "read_bytes", return_value=data):
            redacted = module.extract([Path(source)], redact_args)
            trusted = module.extract([Path(source)], trusted_args)
        if len(redacted) != 1 or len(trusted) != 1:
            raise AssertionError(f"expected one candidate in each mode for {source}")
        if "/" in redacted[0].source_file:
            raise AssertionError(f"redacted source metadata leaked a path: {redacted[0].source_file}")
        for artifact in (
            module.candidate_to_json(redacted[0]),
            list(module.prompt_batches(redacted, redact_args.batch_chars)),
        ):
            if source in json.dumps(artifact):
                raise AssertionError(f"redacted candidate or prompt leaked source path: {source}")
        if trusted[0].source_file != source:
            raise AssertionError(f"trusted originals lost source path: {source}")


def test_redact_mode_records_person_data_privacy_summary() -> None:
    redact_args, module = args(redact=True, trusted_originals=False)
    trusted_args, _module = args(redact=False, trusted_originals=True)
    redacted_privacy = module.privacy_summary(redact_args)
    trusted_privacy = module.privacy_summary(trusted_args)
    if not redacted_privacy.get("redact_person_data"):
        raise AssertionError(f"redact diagnostics should report person-data redaction: {redacted_privacy}")
    if trusted_privacy.get("redact_person_data"):
        raise AssertionError(f"trusted originals should not report person-data redaction: {trusted_privacy}")


def test_redact_mounted_paths_in_text_context_and_prompts() -> None:
    redact_args, module = args(redact=True, trusted_originals=False, max_record_chars=2_000)
    trusted_args, _module = args(max_record_chars=2_000)
    paths = (
        ("/Volumes/EXAMPLE/worktrees/sample", "EXAMPLE/worktrees/sample"),
        ('"/Volumes/Example Disk/Task Evidence/sample"', "Task Evidence/sample"),
        ("`/Volumes/Example Disk/worktrees/sample`", "Disk/worktrees/sample"),
        ("'/mnt/example/task evidence/sample'", "task evidence/sample"),
        ("/media/example/worktrees/sample", "example/worktrees/sample"),
        (r"/Volumes/Example\ Disk/worktrees/sample", "Disk/worktrees/sample"),
        ("file:///Volumes/EXAMPLE/worktrees/sample", "EXAMPLE/worktrees/sample"),
        ("vscode://file/Users/example/sample.py:12", "example/sample.py"),
        ("http://localhost:5173/@fs/Users/example/sample.py", "example/sample.py"),
        ("http://127.0.0.1:5173/@fs/Volumes/EXAMPLE/sample.py", "EXAMPLE/sample.py"),
        ("http://workstation.local/view?path=/Volumes/EXAMPLE/sample.py", "EXAMPLE/sample.py"),
        ("http://192.168.1.2/view?path=/Volumes/EXAMPLE/sample.py", "EXAMPLE/sample.py"),
        ("http://workstation:5173/@fs/Users/example/sample.py", "example/sample.py"),
        ("http://100.101.102.103:5173/@fs/Volumes/EXAMPLE/sample.py", "EXAMPLE/sample.py"),
        ("http://workstation.tail1234.ts.net/view?path=/Volumes/EXAMPLE/sample.py", "EXAMPLE/sample.py"),
        ("http://workstation.home.arpa/view?path=/Volumes/EXAMPLE/sample.py", "EXAMPLE/sample.py"),
        ("http://workstation.test/view?path=/Volumes/EXAMPLE/sample.py", "EXAMPLE/sample.py"),
        ("/Volumes/EXAMPLE/Photos(2024)/sample.png", "Photos(2024)/sample.png"),
        ("/Users/example/[draft]/notes.md", "[draft]/notes.md"),
        ('"/Volumes/Example Disk/worktrees/sample', "Disk/worktrees/sample"),
        ("/Volumes/Example Disk/worktrees/sample", "Disk/worktrees/sample"),
        ("https://example.com/view?path=/Users/example/sample.py", "example/sample.py"),
        ("https://example.com/view#worktree=/Volumes/EXAMPLE/sample.py", "EXAMPLE/sample.py"),
        ("https://example.com/@fs/Users/example/sample.py", "example/sample.py"),
        ("http://workstation.localdomain/Volumes/EXAMPLE/sample.py", "EXAMPLE/sample.py"),
        ("https://vscode.dev/tunnel/workstation/Users/example/sample.py", "example/sample.py"),
    )
    public_url = "https://example.com/Volumes/EXAMPLE/worktrees/sample"
    for path, private_tail in paths:
        messages = (
            f"Remember worktrees live under {path}; keep reusable workflows simple.",
            f"Read {public_url} for details; evidence is in {path}.",
        )
        data = "\n".join(json.dumps(response_item("user", text)) for text in messages).encode()
        with patch.object(Path, "read_bytes", return_value=data):
            redacted = module.extract([Path("/Volumes/EXAMPLE/rollout.jsonl")], redact_args)
            trusted = module.extract([Path("/Volumes/EXAMPLE/rollout.jsonl")], trusted_args)
        if not redacted or not trusted:
            raise AssertionError("expected candidates in both modes")
        text = redacted[0].text
        context = redacted[0].context
        if private_tail in text or any(private_tail in event["text"].replace(public_url, "") for event in context):
            raise AssertionError(f"candidate text/context leaked {path}: {redacted}")
        unterminated_quote = path[0] in ('"', "'", "`") and not path.endswith(path[0])
        if not unterminated_quote and "keep reusable workflows simple." not in text:
            raise AssertionError(f"path redaction consumed neighboring prose: {text}")
        if not any(public_url in event["text"] for event in context):
            raise AssertionError(f"path redaction damaged public URL: {context}")
        prompts = json.dumps(list(module.prompt_batches(redacted, redact_args.batch_chars)))
        if private_tail in prompts.replace(public_url, ""):
            raise AssertionError(f"prompt leaked mounted path: {path}")
        if path not in trusted[0].text or not any(path in event["text"] for event in trusted[0].context):
            raise AssertionError(f"trusted mode lost mounted path: {path}")


def test_redacted_bundle_artifact_references_are_portable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        trace = write_trace(root, [response_item("user", "Remember worktrees live under /Volumes/EXAMPLE/worktrees/sample.")])
        for redact in (True, False):
            namespace, module = args(redact=redact, trusted_originals=not redact)
            out_dir = root / "private bundle" / str(redact)
            candidates = module.extract([trace], namespace)
            diagnostics = module.write_artifacts(out_dir, [trace], candidates, namespace)
            stored = json.loads((out_dir / "diagnostics.json").read_text(encoding="utf-8"))
            if diagnostics != stored or stored["candidate_count"] != len(candidates):
                raise AssertionError("diagnostics must read back with useful counts")
            for reference in stored["artifacts"].values():
                target = Path(reference)
                if redact:
                    if target.is_absolute() or str(root) in reference:
                        raise AssertionError(f"redacted diagnostics leaked output location: {reference}")
                    target = out_dir / target
                if not target.is_file():
                    raise AssertionError(f"artifact reference no longer resolves: {reference}")
            if redact:
                for artifact in out_dir.iterdir():
                    payload = artifact.read_text(encoding="utf-8")
                    if str(root) in payload or "/Volumes/EXAMPLE" in payload:
                        raise AssertionError(f"redacted bundle leaked local path in {artifact.name}")
            elif str(out_dir) not in json.dumps(stored["artifacts"]):
                raise AssertionError("trusted mode should preserve original artifact references")


def test_redact_mode_does_not_overmatch_public_names_or_plain_prose() -> None:
    redact_args, module = args(redact=True, trusted_originals=False)
    text = (
        "GitHub Actions passed after the rollout. "
        "owner repository and manager workflow are ordinary prose here. "
        "github product names should not be treated as handles. "
        "GitHub handle alice-example still needs redaction."
    )

    redacted = module.redact_person_data(text)

    if "GitHub Actions" not in redacted:
        raise AssertionError(f"redact mode mangled GitHub Actions: {redacted}")
    for expected in ("owner repository", "manager workflow", "github product names"):
        if expected not in redacted:
            raise AssertionError(f"redact mode overmatched prose {expected!r}: {redacted}")
    if "alice-example" in redacted or "<person-handle-redacted>" not in redacted:
        raise AssertionError(f"redact mode missed explicit handle: {redacted}")


def test_prompt_batches_emit_destination_aware_task() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(Path(tmp), [response_item("user", "Remember local LLM preference qwen3-coder-64b.")])
        candidates = module.extract([trace], namespace)
    batches = list(module.prompt_batches(candidates, batch_chars=10_000))
    if len(batches) != 1:
        raise AssertionError(f"expected one batch, got {len(batches)}")
    task = batches[0]["task"]
    if "people_updates" not in task or "discard" not in task:
        raise AssertionError(f"prompt task should be destination-aware: {task}")
    if "github_handle" not in task or "aliases" not in task:
        raise AssertionError(f"people prompt should request structured identity fields: {task}")
    people_schema = batches[0]["output_schema"]["people_updates"][0]
    for expected_key in ("name", "aliases", "github_handle", "role", "organization"):
        if expected_key not in people_schema:
            raise AssertionError(f"people schema missing {expected_key}: {people_schema}")
    if batches[0].get("allowed_model_scope") != "trusted_local_or_trusted_lan":
        raise AssertionError(f"prompt should be local/trusted scoped: {batches[0]}")


def test_candidate_ids_are_stable() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(Path(tmp), [response_item("user", "Remember local LLM preference qwen3-coder-64b.")])
        first = module.extract([trace], namespace)
        second = module.extract([trace], namespace)
    if first[0].candidate_id != second[0].candidate_id:
        raise AssertionError("candidate ids should be stable across identical reruns")
    if not first[0].candidate_id.startswith("memcand_"):
        raise AssertionError(f"unexpected candidate id shape: {first[0].candidate_id}")


def test_dedupe_keeps_distinct_long_common_prefixes() -> None:
    namespace, module = args(max_record_chars=2_000)
    prefix = "Remember " + ("shared workflow detail " * 60)
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                response_item("user", prefix + "first durable preference for qwen3-coder-64b."),
                response_item("user", prefix + "second durable preference for qwen3-coder-next."),
            ],
        )
        candidates = module.extract([trace], namespace)
    if len(candidates) != 2:
        raise AssertionError(f"expected both same-prefix candidates to survive dedupe, got {len(candidates)}")


def test_classifies_repo_specific_details() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [response_item("user", "In skill-creator/scripts/build.py keep artifact scans local.")],
        )
        candidates = module.extract([trace], namespace)
    destinations = {candidate.destination for candidate in candidates}
    if "repo-specific" not in destinations:
        raise AssertionError(f"expected repo-specific destination, got {destinations}")


def test_output_dir_writes_local_artifacts() -> None:
    namespace, module = args()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        trace = write_trace(root, [response_item("user", "Remember local LLM preference qwen3-coder-64b.")])
        out_dir = root / "artifacts"
        candidates = module.extract([trace], namespace)
        module.write_artifacts(out_dir, [trace], candidates, namespace)
        candidates_path = out_dir / "candidates.jsonl"
        prompts_path = out_dir / "llm-prompts.jsonl"
        diagnostics_path = out_dir / "diagnostics.json"
        for path in (candidates_path, prompts_path, diagnostics_path):
            if not path.exists():
                raise AssertionError(f"missing artifact {path}")
        first_candidate = json.loads(candidates_path.read_text(encoding="utf-8").splitlines()[0])
        if first_candidate.get("schema_version") != 1 or "candidate_id" not in first_candidate:
            raise AssertionError(f"candidate artifact missing schema/id: {first_candidate}")
        diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
        if diagnostics["privacy"]["local_only"] is not True:
            raise AssertionError(f"diagnostics should mark local_only: {diagnostics}")
        if diagnostics["candidate_count"] != 1:
            raise AssertionError(f"expected one artifact candidate: {diagnostics}")


def test_output_dir_cli_is_quiet() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        trace = write_trace(root, [response_item("user", "Remember local LLM preference qwen3-coder-64b.")])
        out_dir = root / "artifacts"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(trace), "--trusted-originals", "--output-dir", str(out_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
    if result.stdout:
        raise AssertionError(f"output-dir mode should not print local artifact diagnostics: {result.stdout}")


def test_destination_filter_matches_cli_behavior() -> None:
    namespace, module = args(destination=["local-llm"])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        trace = write_trace(
            root,
            [
                response_item("user", "Remember that qwen3-coder-64b is preferred."),
                response_item("user", "Never dump transient PR status into memory."),
            ],
        )
        candidates = module.extract([trace], namespace)
        wanted = set(namespace.destination)
        filtered = [candidate for candidate in candidates if candidate.destination in wanted]
    if not filtered or {candidate.destination for candidate in filtered} != {"local-llm"}:
        raise AssertionError(f"destination filtering should isolate local-llm: {filtered}")


def test_reads_claude_code_messages_and_tool_results() -> None:
    namespace, module = args()

    def record(kind: str, content: object, **extra: object) -> dict[str, object]:
        return {"type": kind, "sessionId": "session-synthetic", "timestamp": "2026-06-01T12:00:00Z",
                "message": {"role": kind, "content": content}, **extra}

    with tempfile.TemporaryDirectory() as tmp:
        trace = write_trace(
            Path(tmp),
            [
                record("user", "qwen3-coder-64b is our preferred local LLM extraction model."),
                record("user", [{"type": "text", "text": "Kyle/HonkHonk is a trusted collaborator."}], isMeta=True),
                record("user", [{"type": "tool_result", "tool_use_id": "toolu_example", "is_error": True, "content": [
                    {"type": "text", "text": "Auto Review loop repeated and blocked the branch until confirm."}]}]),
                {"type": "last-prompt", "sessionId": "session-synthetic",
                 "lastPrompt": "Kyle/HonkHonk is a trusted collaborator."},
            ],
        )
        candidates = module.extract([trace], namespace)
    destinations = sorted(candidate.destination for candidate in candidates)
    if destinations != ["local-llm", "rollout-friction"]:
        raise AssertionError(f"expected Claude Code message and tool result, without injected or bookkeeping records: {destinations}")


def test_linked_status_reference_lowers_confidence_but_cited_preference_stays() -> None:
    module = load_module()
    link = "[catalog#753](https://github.com/OWNER/catalog/pull/753)"
    for status in (
        f"Next time remember {link} runtime checkout is waiting on CI.",
        "Next time remember run https://github.com/OWNER/catalog/actions/runs/12/job/34 failed.",
    ):
        if module.classify(status)[1] != "low":
            raise AssertionError(f"linked status should be low confidence: {status}")
    durable = f"Always prefer {link} runtime checkout style in owner-facing prose."
    if module.classify(durable)[1] != "medium":
        raise AssertionError(f"a preference that cites a linked reference should stay medium: {durable}")


def test_cli_requires_explicit_trace_source() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    if result.returncode != 2 or "provide trace paths or --root" not in result.stderr or result.stdout:
        raise AssertionError(f"omitted source must refuse without scanning: {result}")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_trace(root, [{"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Always prefer uv for Python commands."}]
        }}])
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root)],
                                capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout)
        if payload["source_file_count"] != 1 or not payload["candidates"]:
            raise AssertionError(f"explicit root should extract the synthetic trace: {payload}")


def main() -> int:
    test_cli_requires_explicit_trace_source()
    test_skips_session_meta_base_instructions()
    test_context_window_preserves_neighboring_turns()
    test_classifies_people_local_llm_and_friction()
    test_linked_status_reference_lowers_confidence_but_cited_preference_stays()
    test_redact_mode_removes_paths_and_person_data_but_keeps_trusted_originals()
    test_redact_source_metadata_is_independent_of_path_root()
    test_redact_mounted_paths_in_text_context_and_prompts()
    test_redacted_bundle_artifact_references_are_portable()
    test_redact_mode_records_person_data_privacy_summary()
    test_redact_mode_does_not_overmatch_public_names_or_plain_prose()
    test_prompt_batches_emit_destination_aware_task()
    test_candidate_ids_are_stable()
    test_dedupe_keeps_distinct_long_common_prefixes()
    test_classifies_repo_specific_details()
    test_output_dir_writes_local_artifacts()
    test_output_dir_cli_is_quiet()
    test_destination_filter_matches_cli_behavior()
    test_reads_claude_code_messages_and_tool_results()
    print("ok validate-extract-rollout-memory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
