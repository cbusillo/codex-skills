#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Cluster rollout friction episodes and emit compact trajectory skeletons."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SECRETISH_RE = re.compile(r"(?i)(api[_-]?key|token|password|secret|bearer)['\"]?\s*[:=]\s*\S+")
PATH_RE = re.compile(
    r"(?<![\w:])(?:~[/\\]|\.{1,2}[/\\]|/(?:Users|home|private|var|tmp|opt|Volumes)|[A-Za-z]:\\|[A-Za-z0-9_.-]+/)"
    r"[^\s,;:)]+"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HEX_RE = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
SUCCESS_STEP_RE = re.compile(
    r"\b(success|succeeded|passed|green|mergeable)\b|\bexit[_ -]?code\s*[:=]?\s*0\b|process exited with code 0",
    re.I,
)
FALSE_SUCCESS_RE = re.compile(r"(?i)(?:\bsuccess\b|['\"]success['\"])\s*[:=]\s*(?:false|0|null|no)\b")
FAILURE_STEP_RE = re.compile(
    r"\b(error|failed|failure|exit[_ -]?code\s*[:=]?\s*[1-9][0-9]*|process exited with code [1-9][0-9]*|timed out|timeout|blocked|rate limit|stale)\b",
    re.I,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cluster rollout friction episodes and build trajectory skeletons.")
    parser.add_argument("episodes", type=Path, help="Episode JSONL or JSON object from segment_rollout_episodes.py.")
    parser.add_argument("--top-clusters", type=int, default=30, help="Maximum clusters to emit, ranked by total cost.")
    parser.add_argument("--max-steps", type=int, default=8, help="Maximum representative skeleton steps per cluster.")
    parser.add_argument(
        "--trusted-originals",
        action="store_true",
        help="Keep local path/email/id shapes after obvious secret stripping. Use only for trusted local review.",
    )
    return parser.parse_args()


def load_episodes(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
    else:
        payload = None
    if isinstance(payload, dict):
        object_records = payload.get("episodes", [])
        if not isinstance(object_records, list):
            raise SystemExit("episodes JSON object must contain an episodes array")
        return [episode for episode in object_records if isinstance(episode, dict)]
    line_records: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise SystemExit(f"line {line_no}: episode record must be an object")
        line_records.append(value)
    return line_records


def cluster_key(episode: dict[str, Any]) -> tuple[str, ...]:
    signals = tuple(sorted(str(signal) for signal in episode.get("signals", []) if signal))
    category = str(episode.get("category") or "unknown")
    destination = str(episode.get("recommended_destination") or "unknown")
    return (*signals, f"category:{category}", f"destination:{destination}")


def cluster_id_for(key: tuple[str, ...]) -> str:
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"cl_{digest}"


def cost(episode: dict[str, Any]) -> int:
    value = episode.get("cost_score", 0)
    return value if isinstance(value, int) else 0


def build_clusters(
    episodes: list[dict[str, Any]],
    top_clusters: int,
    max_steps: int,
    trusted_originals: bool,
) -> dict[str, Any]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        grouped[cluster_key(episode)].append(episode)

    clusters = []
    skeletons = []
    for key, items in grouped.items():
        items = sorted(items, key=cost, reverse=True)
        representative = items[0]
        cluster_id = cluster_id_for(key)
        outcome_mix = Counter(str(item.get("outcome") or "unknown") for item in items)
        signal_counts = Counter(signal for item in items for signal in item.get("signals", []))
        tag_counts = Counter(
            tag
            for item in items
            for tag, count in normalized_tag_counts(item).items()
            for _ in range(count)
        )
        skeleton = skeleton_for(cluster_id, representative, len(items), max_steps, trusted_originals)
        skeletons.append(skeleton)
        clusters.append(
            {
                "schema_version": 1,
                "cluster_id": cluster_id,
                "label": cluster_label(signal_counts, representative),
                "signals": sorted(signal_counts),
                "tag_counts": dict(sorted(tag_counts.items())),
                "primary_signal": representative.get("primary_signal"),
                "category": representative.get("category"),
                "recommended_destination": representative.get("recommended_destination"),
                "episode_count": len(items),
                "affected_file_count": len({item.get("source_file_id") for item in items}),
                "total_cost_score": sum(cost(item) for item in items),
                "max_cost_score": cost(representative),
                "outcome_mix": dict(sorted(outcome_mix.items())),
                "representative_episode_id": representative.get("episode_id"),
                "representative_skeleton_id": skeleton["skeleton_id"],
                "likely_cause": representative.get("likely_cause"),
            }
        )

    clusters.sort(key=lambda item: (item["total_cost_score"], item["episode_count"], item["cluster_id"]), reverse=True)
    wanted_ids = {cluster["representative_skeleton_id"] for cluster in clusters[:top_clusters]}
    return {
        "schema_version": 1,
        "cluster_count": min(len(clusters), top_clusters),
        "total_cluster_count": len(clusters),
        "episode_count": len(episodes),
        "clusters": clusters[:top_clusters],
        "skeletons": [skeleton for skeleton in skeletons if skeleton["skeleton_id"] in wanted_ids],
    }


def cluster_label(signal_counts: Counter[str], representative: dict[str, Any]) -> str:
    top = ", ".join(signal for signal, _count in signal_counts.most_common(3))
    outcome = representative.get("outcome") or "unknown"
    return f"{top} ({outcome})"


def normalized_tag_counts(episode: dict[str, Any]) -> dict[str, int]:
    raw_counts = episode.get("tag_counts")
    if isinstance(raw_counts, dict):
        counts: dict[str, int] = {}
        for tag, count in raw_counts.items():
            if not isinstance(tag, str):
                continue
            try:
                numeric_count = int(count)
            except (TypeError, ValueError):
                continue
            if numeric_count > 0:
                counts[tag] = numeric_count
        return counts
    counts = Counter(
        tag
        for hit in episode.get("hits", [])
        if isinstance(hit, dict)
        for tag in hit.get("tags", [])
        if isinstance(tag, str)
    )
    return dict(counts)


def skeleton_for(
    cluster_id: str,
    episode: dict[str, Any],
    episode_count: int,
    max_steps: int,
    trusted_originals: bool,
) -> dict[str, Any]:
    hits = episode.get("hits", [])
    steps: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        snippet = str(hit.get("snippet") or "")
        steps.append(
            {
                "kind": step_kind(str(hit.get("signal") or ""), snippet),
                "signal": hit.get("signal"),
                "line": hit.get("line"),
                "tags": hit.get("tags") if isinstance(hit.get("tags"), list) else [],
                "summary": sanitize(snippet, trusted_originals),
            }
        )
    compacted, elided_count = compact_steps(steps, max_steps)
    skeleton_id = f"sk_{hashlib.sha256(str(episode.get('episode_id')).encode('utf-8')).hexdigest()[:16]}"
    return {
        "schema_version": 1,
        "skeleton_id": skeleton_id,
        "cluster_id": cluster_id,
        "episode_id": episode.get("episode_id"),
        "episode_count": episode_count,
        "outcome": episode.get("outcome"),
        "cost_score": episode.get("cost_score"),
        "steps": compacted,
        "step_count": len(steps),
        "shown_steps": sum(1 for step in compacted if step.get("kind") != "elision"),
        "elided_steps": elided_count,
    }


def step_kind(signal: str, snippet: str) -> str:
    lowered = snippet.lower()
    if signal == "user_context_correction":
        return "user_correction"
    if SUCCESS_STEP_RE.search(snippet) and not FAILURE_STEP_RE.search(snippet) and not FALSE_SUCCESS_RE.search(snippet):
        return "resolution"
    if "retry" in lowered or "rerun" in lowered or "again" in lowered:
        return "retry"
    if "error" in lowered or "failed" in lowered or "blocked" in lowered or "timeout" in lowered:
        return "failure"
    return "signal"


def sanitize(text: str, trusted_originals: bool) -> str:
    cleaned = " ".join(text.split())
    cleaned = SECRETISH_RE.sub("<secret-redacted>", cleaned)
    if not trusted_originals:
        cleaned = PATH_RE.sub("<path-redacted>", cleaned)
        cleaned = EMAIL_RE.sub("<email-redacted>", cleaned)
        cleaned = HEX_RE.sub("<id-redacted>", cleaned)
    if len(cleaned) > 360:
        return cleaned[:357].rstrip() + "..."
    return cleaned


def compact_steps(steps: list[dict[str, Any]], max_steps: int) -> tuple[list[dict[str, Any]], int]:
    max_steps = max(1, max_steps)
    if len(steps) <= max_steps:
        return steps, 0
    if max_steps == 1:
        return [{"kind": "elision", "summary": f"{len(steps)} signal step(s) elided"}], len(steps)
    if max_steps == 2:
        return [
            steps[0],
            {"kind": "elision", "summary": f"{len(steps) - 1} similar or intervening signal step(s) elided"},
        ], len(steps) - 1
    head_count = max(1, max_steps // 2)
    tail_count = max(1, max_steps - head_count - 1)
    omitted = len(steps) - head_count - tail_count
    return [
        *steps[:head_count],
        {"kind": "elision", "summary": f"{omitted} similar or intervening signal step(s) elided"},
        *steps[-tail_count:],
    ], omitted


def main() -> int:
    args = parse_args()
    payload = build_clusters(load_episodes(args.episodes), args.top_clusters, args.max_steps, args.trusted_originals)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
