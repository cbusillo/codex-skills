#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Revalidate and summarize local rollout memory review artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from validate_rollout_memory_llm_results import ValidationError, validate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize reviewed rollout-memory batches.")
    parser.add_argument("review_dir", type=Path)
    parser.add_argument("--json", action="store_true", help="Emit full summary JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = summarize(args.review_dir)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            "rollout-memory-reviews "
            f"batches={summary['batch_count']} ok={summary['ok_count']} failed={summary['failed_count']} "
            f"candidates={summary['candidate_count']} covered={summary['covered_count']} "
            f"notes={summary['note_count']} discards={summary['discard_count']}"
        )
    return 0 if summary["failed_count"] == 0 else 1


def summarize(review_dir: Path) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    child_parents = split_child_parents(review_dir)
    for prompt_path in sorted(review_dir.glob("batch-*.prompt.json")):
        stem = prompt_path.name.removesuffix(".prompt.json")
        result_path = review_dir / f"{stem}.result.json"
        batch_no = stem.removeprefix("batch-")
        if not result_path.exists():
            summaries.append({"ok": False, "batch": batch_no, "error": "missing result", "result": str(result_path)})
            continue
        try:
            validation = validate(prompt_path, result_path)
        except ValidationError as exc:
            validation = {"ok": False, "error": str(exc)}
        validation.update({"batch": batch_no, "prompt": str(prompt_path), "result": str(result_path)})
        if not validation.get("ok") and batch_no in child_parents:
            validation["superseded_by_split_children"] = True
        summaries.append(validation)
    active_summaries = [summary for summary in summaries if not summary.get("superseded_by_split_children")]
    return {
        "schema_version": 1,
        "review_dir": str(review_dir),
        "batch_count": len(active_summaries),
        "superseded_parent_count": len(summaries) - len(active_summaries),
        "ok_count": sum(1 for summary in active_summaries if summary.get("ok")),
        "failed_count": sum(1 for summary in active_summaries if not summary.get("ok")),
        "candidate_count": sum(int(summary.get("candidate_count") or 0) for summary in active_summaries),
        "covered_count": sum(int(summary.get("covered_count") or 0) for summary in active_summaries),
        "note_count": sum(int(summary.get("note_count") or 0) for summary in active_summaries),
        "discard_count": sum(int(summary.get("discard_count") or 0) for summary in active_summaries),
        "failed_batches": [summary for summary in active_summaries if not summary.get("ok")],
        "summaries": summaries,
    }


def split_child_parents(review_dir: Path) -> set[str]:
    parents: set[str] = set()
    for prompt_path in review_dir.glob("batch-*-*.prompt.json"):
        stem = prompt_path.name.removesuffix(".prompt.json").removeprefix("batch-")
        parent, _sep, _suffix = stem.partition("-")
        if parent:
            parents.add(parent)
    return parents


if __name__ == "__main__":
    raise SystemExit(main())
