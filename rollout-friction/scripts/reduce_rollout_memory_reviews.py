#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Reduce validated rollout memory reviews into a local apply-plan draft."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from validate_rollout_memory_llm_results import NOTE_KEYS, ValidationError, validate


DESTINATION_FILES = {
    "people_updates": ".local/people.yaml",
    "profile_notes": ".local/profile.md",
    "rollout_friction_notes": "rollout-friction/SKILL.md or rollout-friction local notes",
    "local_llm_notes": ".local/local-llm.yaml",
    "repo_specific_notes": "repo-specific local notes or owning repo",
}
TEXT_KEYS = ("note", "memory", "text", "summary", "update", "fact")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reduce local rollout-memory reviews into an apply-plan JSON.")
    parser.add_argument("review_dir", type=Path)
    parser.add_argument("--output", type=Path, help="Write apply-plan JSON. Defaults to stdout only.")
    parser.add_argument("--include-failed", action="store_true", help="Include failed batch summaries in the output plan.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = reduce_reviews(args.review_dir, include_failed=args.include_failed)
    text = json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
    print(text)
    return 0 if not plan["failed_batches"] else 1


def reduce_reviews(review_dir: Path, include_failed: bool = False) -> dict[str, Any]:
    notes_by_key: dict[str, list[dict[str, Any]]] = {key: [] for key in NOTE_KEYS}
    discard_count = 0
    failed_batches: list[dict[str, Any]] = []
    reviewed_batches = 0
    child_parents = split_child_parents(review_dir)
    for prompt_path in sorted(review_dir.glob("batch-*.prompt.json")):
        stem = prompt_path.name.removesuffix(".prompt.json")
        result_path = review_dir / f"{stem}.result.json"
        batch_label = stem.removeprefix("batch-")
        if not result_path.exists():
            failed_batches.append({"batch": batch_label, "error": "missing result"})
            continue
        try:
            validation = validate(prompt_path, result_path)
        except ValidationError as exc:
            if batch_label not in child_parents:
                failed_batches.append({"batch": batch_label, "error": str(exc)})
            continue
        if not validation["ok"]:
            if batch_label not in child_parents:
                failed_batches.append({"batch": batch_label, **validation})
            continue
        payload = model_payload(result_path)
        reviewed_batches += 1
        discards = payload.get("discard_reasons")
        discard_count += len(discards) if isinstance(discards, list) else 0
        for key in NOTE_KEYS:
            values = payload.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, dict):
                    notes_by_key[key].append(normalize_note(item, batch_label, key))
    reduced = {key: dedupe_notes(values) for key, values in notes_by_key.items()}
    return {
        "schema_version": 1,
        "review_dir": str(review_dir),
        "reviewed_batch_count": reviewed_batches,
        "failed_batch_count": len(failed_batches),
        "failed_batches": failed_batches if include_failed or failed_batches else [],
        "discard_count": discard_count,
        "destinations": {
            key: {
                "target": DESTINATION_FILES[key],
                "count": len(values),
                "updates": values,
            }
            for key, values in reduced.items()
        },
        "apply_guidance": "Review this draft manually before editing local memory or repo files.",
    }


def model_payload(result_path: Path) -> dict[str, Any]:
    with result_path.open("r", encoding="utf-8") as handle:
        envelope = json.load(handle)
    content = envelope.get("content")
    if not isinstance(content, str):
        raise ValidationError("result content is not a string")
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValidationError("result content is not an object")
    return payload


def normalize_note(item: dict[str, Any], batch_label: str, bucket: str) -> dict[str, Any]:
    text = note_text(item)
    candidate_ids = candidate_ids_from_note(item)
    normalized: dict[str, Any] = {
        "id": stable_note_id(bucket, text, candidate_ids),
        "bucket": bucket,
        "text": text,
        "candidate_ids": candidate_ids,
        "source_batches": [batch_label],
        "raw_keys": sorted(str(key) for key in item.keys()),
    }
    confidence = item.get("confidence")
    if isinstance(confidence, (int, float, str)):
        normalized["confidence"] = confidence
    reason = item.get("reason") or item.get("rationale")
    if isinstance(reason, str) and reason.strip():
        normalized["reason"] = " ".join(reason.split())
    return normalized


def note_text(item: dict[str, Any]) -> str:
    for key in TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def candidate_ids_from_note(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("candidate_id", "source_candidate_id", "candidate_ids", "source_candidate_ids"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
        elif isinstance(value, list):
            values.extend(child for child in value if isinstance(child, str) and child.strip())
    return sorted(dict.fromkeys(values))


def stable_note_id(bucket: str, text: str, candidate_ids: list[str]) -> str:
    payload = json.dumps({"bucket": bucket, "text": text.casefold(), "candidate_ids": candidate_ids}, sort_keys=True)
    return "memnote_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def dedupe_notes(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for note in notes:
        key = canonical_note_key(note)
        if key not in by_key:
            by_key[key] = dict(note)
            continue
        existing = by_key[key]
        existing["candidate_ids"] = sorted(set(existing["candidate_ids"]) | set(note["candidate_ids"]))
        existing["source_batches"] = sorted(set(existing["source_batches"]) | set(note["source_batches"]))
    return sorted(by_key.values(), key=lambda item: (item["bucket"], item["text"]))


def canonical_note_key(note: dict[str, Any]) -> str:
    return " ".join(str(note.get("text") or "").casefold().split())


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
