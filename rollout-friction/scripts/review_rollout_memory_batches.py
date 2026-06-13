#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""Run trusted-local LLM review over rollout memory prompt batches."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

LOCAL_LLM_SCRIPTS = Path(__file__).resolve().parents[2] / "local-llm" / "scripts"
if str(LOCAL_LLM_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(LOCAL_LLM_SCRIPTS))

from lm_studio_api import (  # type: ignore[import-not-found]  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_INDEX,
    LocalLLMError,
    load_lm_studio_model,
    load_yaml,
    parse_float_option,
    parse_int_option,
    post_json,
    public_endpoint,
    resolve_endpoint,
    resolve_role,
    role_model,
    unload_lm_studio_model,
)
from validate_rollout_memory_llm_results import ValidationError, validate


DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "qwen3-coder-64b"
DEFAULT_TIMEOUT = 300.0
DEFAULT_MAX_TOKENS = 7_000
DEFAULT_MAX_INPUT_CHARS = 220_000
DEFAULT_SYSTEM = (
    "You are a trusted local-only memory extraction reviewer. Return only valid JSON "
    "matching the requested schema. Prefer discard unless a candidate is durable, "
    "actionable, and supported by explicit user intent. Do not quote private snippets. "
    "Return one decisions item for every candidate_id exactly once. Mirror note decisions "
    "into the matching note list and discard decisions into discard_reasons. Do not put "
    "a candidate_id in both a note list and discard_reasons. Include every candidate_id "
    "in reviewed_candidate_ids."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review rollout memory prompt batches with a local LLM.")
    parser.add_argument("prompts", type=Path, help="llm-prompts.jsonl from extract_rollout_memory.py")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--endpoint", help="Endpoint id from private local-llm config.")
    parser.add_argument("--role", help="Role from local-llm model index/private config.")
    parser.add_argument("--base-url", help="Override endpoint base URL.")
    parser.add_argument("--model", help="Explicit model id. Overrides --role.")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-input-chars", type=int, default=DEFAULT_MAX_INPUT_CHARS)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--load-policy", choices=("none", "jit_chat", "api_explicit"), help="Model lifecycle policy. Defaults to role load_policy or none.")
    parser.add_argument("--ttl", type=int, help="TTL seconds for LM Studio JIT chat loading.")
    parser.add_argument("--context-length", type=int, help="Context length for LM Studio api_explicit load policy.")
    parser.add_argument("--flash-attention", action="store_true", help="Request flash attention for LM Studio api_explicit load.")
    parser.add_argument("--warmup", action="store_true", help="Run a harmless readiness probe before private prompt batches.")
    parser.add_argument("--unload-after", action="store_true", help="Unload the instance loaded by api_explicit after all batches.")
    parser.add_argument("--start-batch", type=int)
    parser.add_argument("--end-batch", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retry-incomplete", type=int, default=1)
    parser.add_argument(
        "--split-on-failure",
        action="store_true",
        help="When a full batch fails, retry deterministic child batches split from the original prompt.",
    )
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--system", default=DEFAULT_SYSTEM)
    args = parser.parse_args()
    if args.max_input_chars <= 0:
        parser.error("--max-input-chars must be positive")
    if args.max_tokens is not None and args.max_tokens <= 0:
        parser.error("--max-tokens must be positive")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.retry_incomplete < 0:
        parser.error("--retry-incomplete must be zero or positive")
    return args


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    runtime: dict[str, Any] | None = None
    summaries: list[dict[str, Any]] = []
    try:
        runtime = configure_runtime(args)
        if args.warmup:
            warmup(runtime, args)
        batches = selected_batches(read_jsonl(args.prompts), args)
        for batch in batches:
            batch_no = int(batch.get("batch") or len(summaries) + 1)
            summary = review_batch(batch, batch_no, args, runtime)
            if args.split_on_failure and not summary.get("ok"):
                summary = review_split_batch(batch, batch_no, args, runtime, summary)
            summaries.append(summary)
            print(json.dumps(summary, sort_keys=True), flush=True)
    except (LocalLLMError, LocalReviewError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), flush=True)
        return 1
    finally:
        if runtime is not None:
            unload_after(args, runtime)
    if runtime is None:
        return 1
    write_summary(args.output_dir / "summary.json", args, runtime, summaries)
    lifecycle = lifecycle_for(runtime)
    ok = all(summary.get("ok") for summary in summaries) and not lifecycle.get("unload_error")
    return 0 if ok else 1


def configure_runtime(args: argparse.Namespace) -> dict[str, Any]:
    config = load_yaml(args.config)
    index = load_yaml(MODEL_INDEX)
    role = resolve_role(config, index, args.role)
    endpoint = resolve_endpoint(config, args.endpoint, args.base_url, role)
    model = args.model or role_model(role) or DEFAULT_MODEL
    args.model = model
    args.timeout = parse_float_option(args.timeout, role.get("timeout_seconds"), DEFAULT_TIMEOUT, "timeout")
    args.max_tokens = parse_int_option(args.max_tokens, role.get("max_tokens"), DEFAULT_MAX_TOKENS, "max_tokens")
    args.temperature = parse_float_option(args.temperature, role.get("temperature"), 0.1, "temperature")
    lifecycle = prepare_lifecycle(endpoint, model, args, role)
    return {"endpoint": endpoint, "role": role, "model": model, "lifecycle": lifecycle}


def prepare_lifecycle(endpoint: dict[str, Any], model: str, args: argparse.Namespace, role: dict[str, Any]) -> dict[str, Any]:
    raw_load_config = role.get("load")
    load_config: dict[str, Any] = raw_load_config if isinstance(raw_load_config, dict) else {}
    policy = normalize_load_policy(args.load_policy or str(role.get("load_policy") or load_config.get("policy") or "none"))
    ttl_source = role.get("ttl_seconds") or load_config.get("ttl_seconds")
    ttl = parse_int_option(args.ttl, ttl_source, 0, "ttl") if args.ttl or ttl_source else None
    lifecycle: dict[str, Any] = {"load_policy": policy, "ttl_seconds": ttl}
    if policy == "none":
        return lifecycle
    if policy == "jit_chat":
        if endpoint.get("provider") != "lm_studio":
            raise LocalReviewError("jit_chat load policy requires provider=lm_studio")
        return lifecycle
    if policy == "api_explicit":
        context_source = role.get("context_length") or load_config.get("context_length")
        context_length = (
            parse_int_option(args.context_length, context_source, 0, "context_length")
            if args.context_length or context_source
            else None
        )
        flash_attention = True if args.flash_attention else load_config.get("flash_attention")
        if ttl:
            lifecycle["ttl_note"] = "ttl is not sent to LM Studio native load; use --unload-after for explicit cleanup"
        response = load_lm_studio_model(
            endpoint,
            model,
            args.timeout,
            context_length=context_length,
            flash_attention=flash_attention,
            echo_load_config=True,
        )
        lifecycle.update(
            {
                "loaded_instance_id": response.get("instance_id"),
                "load_response": {
                    "status": response.get("status"),
                    "load_time_seconds": response.get("load_time_seconds"),
                    "load_config": response.get("load_config"),
                },
            }
        )
        return lifecycle
    raise LocalReviewError(f"unknown load policy: {policy}")


def normalize_load_policy(policy: str) -> str:
    aliases = {"jit": "jit_chat", "explicit": "api_explicit", "native": "api_explicit"}
    return aliases.get(policy, policy)


def warmup(runtime: dict[str, Any], args: argparse.Namespace) -> None:
    endpoint = runtime["endpoint"]
    payload: dict[str, Any] = {
        "model": runtime["model"],
        "messages": [
            {"role": "system", "content": "You are a readiness probe."},
            {"role": "user", "content": "Reply with exactly: OK"},
        ],
        "temperature": 0,
        "max_tokens": 16,
        "stream": False,
    }
    lifecycle = lifecycle_for(runtime)
    if lifecycle.get("load_policy") == "jit_chat" and lifecycle.get("ttl_seconds"):
        payload["ttl"] = lifecycle["ttl_seconds"]
    response = post_json(
        f"{endpoint['base_url']}/chat/completions",
        payload,
        endpoint,
        args.timeout,
        error_context="warmup chat request failed",
    )
    lifecycle["warmup_served_model"] = response.get("model")


def unload_after(args: argparse.Namespace, runtime: dict[str, Any]) -> None:
    lifecycle = lifecycle_for(runtime)
    instance_id = lifecycle.get("loaded_instance_id")
    if not args.unload_after or not instance_id:
        return
    try:
        lifecycle["unload_response"] = unload_lm_studio_model(runtime["endpoint"], str(instance_id), args.timeout)
    except LocalLLMError as exc:
        lifecycle["unload_error"] = str(exc)


def lifecycle_for(runtime: dict[str, Any]) -> dict[str, Any]:
    lifecycle = runtime.get("lifecycle")
    return lifecycle if isinstance(lifecycle, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError(f"line {line_no} is not an object")
                rows.append(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"error: unable to read prompt JSONL: {exc}") from exc
    return rows


def selected_batches(batches: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for batch in batches:
        batch_no = int(batch.get("batch") or 0)
        if args.start_batch is not None and batch_no < args.start_batch:
            continue
        if args.end_batch is not None and batch_no > args.end_batch:
            continue
        selected.append(batch)
        if args.limit is not None and len(selected) >= args.limit:
            break
    return selected


def review_batch(batch: dict[str, Any], batch_no: int | str, args: argparse.Namespace, runtime: dict[str, Any]) -> dict[str, Any]:
    batch_label = batch_label_for_path(batch_no)
    prompt_path = args.output_dir / f"batch-{batch_label}.prompt.json"
    result_path = args.output_dir / f"batch-{batch_label}.result.json"
    validation_path = args.output_dir / f"batch-{batch_label}.validation.json"
    write_json(prompt_path, batch)
    if args.skip_existing and result_path.exists():
        existing = validate_existing_result(prompt_path, validation_path, batch_no, result_path)
        if existing.get("ok"):
            return existing

    attempts = args.retry_incomplete + 1
    last_summary: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            envelope = chat(batch, args, runtime, retry_attempt=attempt)
        except PromptTooLargeError as exc:
            last_summary = {
                "ok": False,
                "batch": batch_no,
                "attempt": attempt,
                "error": str(exc),
                "prompt_too_large": True,
            }
            write_json(validation_path, last_summary)
            break
        envelope["elapsed_seconds"] = round(time.time() - started, 3)
        envelope["attempt"] = attempt
        write_json(result_path, envelope)
        if not envelope.get("ok"):
            last_summary = {
                "ok": False,
                "batch": batch_no,
                "attempt": attempt,
                "error": envelope.get("error", "unknown local LLM error"),
            }
            write_json(validation_path, last_summary)
            continue
        try:
            validation = validate(prompt_path, result_path)
        except ValidationError as exc:
            validation = {
                "ok": False,
                "batch": batch_no,
                "attempt": attempt,
                "result": str(result_path),
                "error": str(exc),
            }
            write_json(validation_path, validation)
            last_summary = validation
            continue
        validation.update({"batch": batch_no, "attempt": attempt, "result": str(result_path)})
        write_json(validation_path, validation)
        last_summary = validation
        if validation["ok"]:
            break
    return last_summary


def review_split_batch(
    batch: dict[str, Any],
    batch_no: int,
    args: argparse.Namespace,
    runtime: dict[str, Any],
    parent_summary: dict[str, Any],
) -> dict[str, Any]:
    candidates = batch.get("candidates")
    if not isinstance(candidates, list) or len(candidates) <= 1:
        return parent_summary
    split_summaries: list[dict[str, Any]] = []
    for suffix, child in split_batch(batch):
        child_batch_no = f"{batch_label_for_path(batch_no)}-{suffix}"
        split_summaries.append(review_batch(child, child_batch_no, args, runtime))
    ok = all(summary.get("ok") for summary in split_summaries)
    return {
        "ok": ok,
        "batch": batch_no,
        "split": True,
        "parent": parent_summary,
        "child_count": len(split_summaries),
        "candidate_count": sum(int(summary.get("candidate_count") or 0) for summary in split_summaries),
        "covered_count": sum(int(summary.get("covered_count") or 0) for summary in split_summaries),
        "note_count": sum(int(summary.get("note_count") or 0) for summary in split_summaries),
        "discard_count": sum(int(summary.get("discard_count") or 0) for summary in split_summaries),
        "children": split_summaries,
    }


def split_batch(batch: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    candidates = batch.get("candidates")
    if not isinstance(candidates, list) or len(candidates) <= 1:
        return []
    midpoint = max(1, len(candidates) // 2)
    parts = [("a", candidates[:midpoint]), ("b", candidates[midpoint:])]
    children: list[tuple[str, dict[str, Any]]] = []
    for suffix, child_candidates in parts:
        child = dict(batch)
        child["parent_batch"] = batch.get("batch")
        child["batch"] = f"{batch.get('batch')}-{suffix}"
        child["candidates"] = child_candidates
        children.append((suffix, child))
    return children


def batch_label_for_path(batch_no: int | str) -> str:
    if isinstance(batch_no, int):
        return f"{batch_no:03d}"
    return str(batch_no)


def chat(batch: dict[str, Any], args: argparse.Namespace, runtime: dict[str, Any], retry_attempt: int) -> dict[str, Any]:
    prompt = json.dumps(batch, ensure_ascii=False)
    if len(prompt) > args.max_input_chars:
        raise PromptTooLargeError(
            f"prompt is {len(prompt)} chars, exceeds --max-input-chars={args.max_input_chars}; split the batch"
        )
    payload = local_review_payload(prompt, args, runtime, retry_attempt)
    endpoint = runtime["endpoint"]
    try:
        response = post_json(
            f"{endpoint['base_url']}/chat/completions",
            payload,
            endpoint,
            args.timeout,
            error_context="rollout memory review chat request failed",
        )
    except LocalLLMError as exc:
        return {"ok": False, "model": args.model, "endpoint": public_endpoint(endpoint), "error": str(exc)}
    content = extract_content(response)
    return {
        "ok": bool(content),
        "model": args.model,
        "served_model": response.get("model"),
        "endpoint": public_endpoint(endpoint),
        "lifecycle": runtime.get("lifecycle"),
        "prompt_chars": len(prompt),
        "max_tokens": args.max_tokens,
        "content": content,
        "error": None if content else "endpoint returned no assistant content",
    }


def local_review_payload(prompt: str, args: argparse.Namespace, runtime: dict[str, Any], retry_attempt: int) -> dict[str, Any]:
    system = args.system
    if retry_attempt > 1:
        system += " Previous output was incomplete. Re-review the batch and cover every candidate_id."
    payload: dict[str, Any] = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "stream": False,
    }
    lifecycle = lifecycle_for(runtime)
    if lifecycle.get("load_policy") == "jit_chat" and lifecycle.get("ttl_seconds"):
        payload["ttl"] = lifecycle["ttl_seconds"]
    return payload


class LocalReviewError(RuntimeError):
    pass


class PromptTooLargeError(LocalReviewError):
    pass


def extract_content(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def write_json(path: Path, payload: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def validate_existing_result(
    prompt_path: Path, validation_path: Path, batch_no: int | str, result_path: Path
) -> dict[str, Any]:
    try:
        validation = validate(prompt_path, result_path)
    except ValidationError as exc:
        validation = {"ok": False, "error": str(exc)}
    validation.update({"batch": batch_no, "attempt": "existing", "result": str(result_path)})
    write_json(validation_path, validation)
    return validation


def write_summary(path: Path, args: argparse.Namespace, runtime: dict[str, Any], summaries: list[dict[str, Any]]) -> None:
    payload = {
        "schema_version": 1,
        "prompt_file": str(args.prompts),
        "model": args.model,
        "endpoint": public_endpoint(runtime["endpoint"]),
        "role": runtime.get("role", {}).get("name"),
        "lifecycle": runtime.get("lifecycle"),
        "batch_count": len(summaries),
        "ok_count": sum(1 for summary in summaries if summary.get("ok")),
        "failed_count": sum(1 for summary in summaries if not summary.get("ok")),
        "candidate_count": sum(int(summary.get("candidate_count") or 0) for summary in summaries),
        "covered_count": sum(int(summary.get("covered_count") or 0) for summary in summaries),
        "note_count": sum(int(summary.get("note_count") or 0) for summary in summaries),
        "discard_count": sum(int(summary.get("discard_count") or 0) for summary in summaries),
        "summaries": summaries,
    }
    write_json(path, payload)


if __name__ == "__main__":
    raise SystemExit(main())
