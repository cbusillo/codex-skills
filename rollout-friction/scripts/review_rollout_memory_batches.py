#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run trusted-local LLM review over rollout memory prompt batches."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

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
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--max-input-chars", type=int, default=DEFAULT_MAX_INPUT_CHARS)
    parser.add_argument("--temperature", type=float, default=0.1)
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
    if args.max_tokens <= 0:
        parser.error("--max-tokens must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.retry_incomplete < 0:
        parser.error("--retry-incomplete must be zero or positive")
    return args


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    batches = selected_batches(read_jsonl(args.prompts), args)
    summaries: list[dict[str, Any]] = []
    for batch in batches:
        batch_no = int(batch.get("batch") or len(summaries) + 1)
        summary = review_batch(batch, batch_no, args)
        if args.split_on_failure and not summary.get("ok"):
            summary = review_split_batch(batch, batch_no, args, summary)
        summaries.append(summary)
        print(json.dumps(summary, sort_keys=True), flush=True)
    write_summary(args.output_dir / "summary.json", args, summaries)
    return 0 if all(summary.get("ok") for summary in summaries) else 1


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


def review_batch(batch: dict[str, Any], batch_no: int | str, args: argparse.Namespace) -> dict[str, Any]:
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
            envelope = chat(batch, args, retry_attempt=attempt)
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
    batch: dict[str, Any], batch_no: int, args: argparse.Namespace, parent_summary: dict[str, Any]
) -> dict[str, Any]:
    candidates = batch.get("candidates")
    if not isinstance(candidates, list) or len(candidates) <= 1:
        return parent_summary
    split_summaries: list[dict[str, Any]] = []
    for suffix, child in split_batch(batch):
        child_batch_no = f"{batch_label_for_path(batch_no)}-{suffix}"
        split_summaries.append(review_batch(child, child_batch_no, args))
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


def chat(batch: dict[str, Any], args: argparse.Namespace, retry_attempt: int) -> dict[str, Any]:
    prompt = json.dumps(batch, ensure_ascii=False)
    if len(prompt) > args.max_input_chars:
        raise PromptTooLargeError(
            f"prompt is {len(prompt)} chars, exceeds --max-input-chars={args.max_input_chars}; split the batch"
        )
    system = args.system
    if retry_attempt > 1:
        system += " Previous output was incomplete. Re-review the batch and cover every candidate_id."
    payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "stream": False,
    }
    try:
        response = post_json(f"{args.base_url.rstrip('/')}/chat/completions", payload, args.timeout)
    except LocalReviewError as exc:
        return {"ok": False, "model": args.model, "error": str(exc)}
    content = extract_content(response)
    return {
        "ok": bool(content),
        "model": args.model,
        "prompt_chars": len(prompt),
        "max_tokens": args.max_tokens,
        "content": content,
        "error": None if content else "endpoint returned no assistant content",
    }


class LocalReviewError(RuntimeError):
    pass


class PromptTooLargeError(LocalReviewError):
    pass


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except TimeoutError as exc:
        raise LocalReviewError(f"request timed out after {timeout:g}s") from exc
    except urllib.error.URLError as exc:
        raise LocalReviewError(f"request failed: {exc.reason}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LocalReviewError(f"response was not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LocalReviewError("response JSON was not an object")
    error = parsed.get("error")
    if error:
        raise LocalReviewError(f"model error: {error}")
    return parsed


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


def write_summary(path: Path, args: argparse.Namespace, summaries: list[dict[str, Any]]) -> None:
    payload = {
        "schema_version": 1,
        "prompt_file": str(args.prompts),
        "model": args.model,
        "base_url": args.base_url,
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
