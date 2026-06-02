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
import re
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
PEOPLE_QUERY_KEYS = (
    "name",
    "display_name",
    "preferred_reference",
    "person",
    "contact",
    "handle",
    "github",
    "github_handle",
    "username",
    "alias",
    "aliases",
)
HANDLE_RE = re.compile(r"(?<![\w/])@[A-Za-z0-9][A-Za-z0-9_-]{1,38}\b")
DEFAULT_SHORTLIST_LIMIT = 30
BUCKET_PRIORITY = {
    "profile_notes": 5,
    "people_updates": 4,
    "local_llm_notes": 3,
    "rollout_friction_notes": 2,
    "repo_specific_notes": 1,
}
STALE_RE = re.compile(
    r"\b(PR #?\d+|issue #?\d+|merged|closed|checks? passed|green checks?|commit [0-9a-f]{7,}|"
    r"branch is clean|worktree branch|ready for PR|opened PR|review identified|reviewer identified|"
    r"focused validation already passed|only file modified|cargo test|pytest|uv run|npm test)\b",
    re.I,
)
KEEPER_RE = re.compile(
    r"\b(prefers?|canonical|must|should|do not|never|always|policy|routing|belongs?|"
    r"primary|fallback|local-only|privacy|protected|non-negotiable)\b",
    re.I,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reduce local rollout-memory reviews into an apply-plan JSON.")
    parser.add_argument("review_dir", type=Path)
    parser.add_argument("--output", type=Path, help="Write apply-plan JSON. Defaults to stdout only.")
    parser.add_argument(
        "--format",
        choices=["plan", "shortlist"],
        default="plan",
        help="Output the full apply-plan JSON or only the curated shortlist JSON.",
    )
    parser.add_argument("--include-failed", action="store_true", help="Include failed batch summaries in the output plan.")
    parser.add_argument(
        "--shortlist-limit",
        type=int,
        default=DEFAULT_SHORTLIST_LIMIT,
        help=f"Maximum curated shortlist updates to include. Default: {DEFAULT_SHORTLIST_LIMIT}.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = reduce_reviews(args.review_dir, include_failed=args.include_failed, shortlist_limit=args.shortlist_limit)
    output = plan if args.format == "plan" else plan["curated_shortlist"]
    text = json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
    print(text)
    return 0 if plan["failed_batch_count"] == 0 else 1


def reduce_reviews(
    review_dir: Path, include_failed: bool = False, shortlist_limit: int = DEFAULT_SHORTLIST_LIMIT
) -> dict[str, Any]:
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
            if batch_label not in child_parents:
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
    people_resolver_smoke_checks = people_smoke_checks(reduced.get("people_updates", []))
    shortlist = curate_shortlist(reduced, shortlist_limit)
    return {
        "schema_version": 1,
        "review_dir": str(review_dir),
        "reviewed_batch_count": reviewed_batches,
        "failed_batch_count": len(failed_batches),
        "failed_batches": failed_batches if include_failed else [],
        "discard_count": discard_count,
        "destinations": {
            key: {
                "target": DESTINATION_FILES[key],
                "count": len(values),
                "updates": values,
            }
            for key, values in reduced.items()
        },
        "curated_shortlist": {
            "limit": shortlist_limit,
            "count": len(shortlist),
            "updates": shortlist,
            "guidance": (
                "Start manual review here. This shortlist favors durable profile/people/workflow facts, "
                "multi-candidate support, and non-transient wording. It is advisory, not an auto-apply list."
            ),
        },
        "people_resolver_smoke_checks": people_resolver_smoke_checks,
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
    if bucket == "people_updates":
        queries = people_queries_from_note(item, text)
        if queries:
            normalized["resolver_queries"] = queries
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
        existing["resolver_queries"] = sorted(
            set(existing.get("resolver_queries") or []) | set(note.get("resolver_queries") or []),
            key=str.casefold,
        )
    return sorted((annotate_note(item) for item in by_key.values()), key=lambda item: (item["bucket"], item["text"]))


def annotate_note(note: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(note)
    annotated["support_count"] = len(annotated.get("candidate_ids") or [])
    annotated["stale_or_transient"] = is_stale_or_transient(str(annotated.get("text") or ""))
    annotated["keeper_score"] = keeper_score(annotated)
    annotated["topic_key"] = topic_key(str(annotated.get("text") or ""))
    return annotated


def curate_shortlist(reduced: dict[str, list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    candidates = [note for notes in reduced.values() for note in notes]
    ranked = sorted(
        candidates,
        key=lambda note: (
            -int(note.get("keeper_score") or 0),
            bool(note.get("stale_or_transient")),
            str(note.get("bucket") or ""),
            str(note.get("text") or ""),
        ),
    )
    selected: list[dict[str, Any]] = []
    topic_keys: list[set[str]] = []
    bucket_counts: dict[str, int] = {}
    for note in ranked:
        topic = topic_terms(str(note.get("text") or ""))
        bucket = str(note.get("bucket") or "")
        if is_near_duplicate_topic(topic, topic_keys):
            continue
        if bucket_counts.get(bucket, 0) >= max(3, limit // 3):
            continue
        selected.append(shortlist_note(note))
        topic_keys.append(topic)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def shortlist_note(note: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: note[key]
        for key in (
            "id",
            "bucket",
            "text",
            "candidate_ids",
            "source_batches",
            "support_count",
            "keeper_score",
            "stale_or_transient",
        )
        if key in note
    }
    if "resolver_queries" in note:
        result["resolver_queries"] = note["resolver_queries"]
    return result


def people_smoke_checks(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    for note in notes:
        for query in note.get("resolver_queries") or []:
            if not isinstance(query, str) or not query.strip():
                continue
            key = query.casefold()
            entry = checks.setdefault(
                key,
                {
                    "query": query,
                    "note_ids": [],
                    "candidate_ids": [],
                    "source_batches": [],
                },
            )
            entry["note_ids"].append(note["id"])
            entry["candidate_ids"].extend(note.get("candidate_ids") or [])
            entry["source_batches"].extend(note.get("source_batches") or [])
    for entry in checks.values():
        entry["note_ids"] = sorted(set(entry["note_ids"]))
        entry["candidate_ids"] = sorted(set(entry["candidate_ids"]))
        entry["source_batches"] = sorted(set(entry["source_batches"]))
    return sorted(checks.values(), key=lambda item: str(item["query"]).casefold())


def people_queries_from_note(item: dict[str, Any], text: str) -> list[str]:
    queries: list[str] = []
    for key in PEOPLE_QUERY_KEYS:
        collect_people_query_value(queries, item.get(key))
    for match in HANDLE_RE.findall(text):
        queries.append(match)
        queries.append(match[1:])
    cleaned: list[str] = []
    for query in queries:
        clean_query = clean_people_query(query)
        if clean_query:
            cleaned.append(clean_query)
    return sorted(dict.fromkeys(cleaned), key=str.casefold)


def collect_people_query_value(queries: list[str], value: object) -> None:
    if isinstance(value, str):
        queries.extend(split_people_query_text(value))
    elif isinstance(value, list):
        for child in value:
            collect_people_query_value(queries, child)
    elif isinstance(value, dict):
        for key in PEOPLE_QUERY_KEYS:
            collect_people_query_value(queries, value.get(key))


def split_people_query_text(value: str) -> list[str]:
    stripped = value.strip()
    if not stripped:
        return []
    parts = re.split(r"[,;]", stripped)
    return [part.strip() for part in parts if part.strip()]


def clean_people_query(value: str) -> str | None:
    query = " ".join(value.strip().split())
    if not query:
        return None
    if len(query) > 80:
        return None
    lowered = query.casefold()
    if lowered in {"github", "user", "reviewer", "manager", "collaborator", "person"}:
        return None
    return query


def keeper_score(note: dict[str, Any]) -> int:
    text = str(note.get("text") or "")
    score = BUCKET_PRIORITY.get(str(note.get("bucket") or ""), 0) * 10
    score += min(len(note.get("candidate_ids") or []), 5) * 3
    if KEEPER_RE.search(text):
        score += 8
    if is_stale_or_transient(text):
        score -= 20
    return score


def is_stale_or_transient(text: str) -> bool:
    return bool(STALE_RE.search(text))


def topic_key(text: str) -> str:
    return " ".join(sorted(topic_terms(text)))[:96]


def topic_terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9_.#-]+", text.casefold())
    stop = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "should",
        "must",
        "user",
        "prefers",
        "prefer",
        "keep",
        "use",
    }
    return {word for word in words if len(word) > 2 and word not in stop}


def is_near_duplicate_topic(topic: set[str], existing_topics: list[set[str]]) -> bool:
    if not topic:
        return False
    for existing in existing_topics:
        overlap = len(topic & existing) / max(1, min(len(topic), len(existing)))
        if overlap >= 0.65:
            return True
    return False


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
