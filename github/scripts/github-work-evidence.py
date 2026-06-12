#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""Collect bounded read-only GitHub work evidence as JSON."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
COLLECTOR_PATH = ROOT / "github-work-rollup/scripts/github_work_rollup.py"
DEFAULT_CONFIG = ROOT / ".local/github-work-rollup.yaml"
NO_PEOPLE_INDEX = ROOT / ".local/github-work-evidence.no-people.yaml"
SCHEMA_VERSION = 1


def load_rollup_module() -> Any:
    spec = importlib.util.spec_from_file_location("github_work_rollup", COLLECTOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load collector module from {COLLECTOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rollup = load_rollup_module()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect read-only GitHub work evidence as JSON.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Optional local YAML config.")
    parser.add_argument("--repo", action="append", default=[], help="OWNER/REPO. May be repeated.")
    parser.add_argument("--repo-owner", action="append", default=[], help="Owner/org whose repos should be scanned.")
    parser.add_argument("--subject", action="append", default=[], help="GitHub login to highlight. May be repeated.")
    parser.add_argument("--window", help="Lookback window such as 24h, 7d, or 1w.")
    parser.add_argument("--since", help="UTC ISO timestamp for window start.")
    parser.add_argument("--until", help="UTC ISO timestamp for window end.")
    parser.add_argument("--timezone", help="IANA timezone label for display metadata.")
    parser.add_argument("--mode", choices=sorted(rollup.REPORT_MODES), help="activity, backlog, or standup.")
    parser.add_argument("--output", type=Path, help="Write JSON evidence to this path.")
    parser.add_argument("--limit-repos", type=int, default=25)
    parser.add_argument("--limit-items", type=int, default=50)
    parser.add_argument("--collection-limit-items", type=int, help="Maximum PRs/issues to collect per repo/state.")
    parser.add_argument("--release-collection-limit", type=int, help="Maximum releases to collect per repo before window filtering.")
    parser.add_argument("--workflow-collection-limit", type=int, help="Maximum workflow runs to collect per repo before window filtering.")
    parser.add_argument("--include-bots", action="store_true")
    parser.add_argument("--include-external-activity", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = rollup.load_config(args.config)
    settings = resolve_evidence_settings(args, config)
    try:
        payload = rollup.collect_rollup(settings)
    except rollup.RollupError as exc:
        payload = rollup.failure_payload(settings, str(exc))
        evidence = evidence_failure(payload)
        write_or_print_json(evidence, args.output)
        return 1
    evidence = evidence_payload(payload, settings)
    write_or_print_json(evidence, args.output)
    return 0


def resolve_evidence_settings(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    compatibility_config = dict(config)
    ignored_audience_keys = {
        "layout",
        "summary_level",
        "report_recipient",
        "people_index",
        "output_path",
    }
    for key in ignored_audience_keys:
        compatibility_config.pop(key, None)
    settings = rollup.resolve_settings(evidence_args(args), compatibility_config)
    settings["evidence_ignored_config_keys"] = sorted(key for key in ignored_audience_keys if key in config)
    return settings


def evidence_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        config=args.config,
        repo=args.repo,
        repo_owner=args.repo_owner,
        subject=args.subject,
        window=args.window,
        since=args.since,
        until=args.until,
        timezone=args.timezone,
        report_recipient="GitHub evidence",
        people_index=NO_PEOPLE_INDEX,
        mode=args.mode,
        summary_level="standard",
        layout="operator",
        output=args.output,
        limit_repos=args.limit_repos,
        limit_items=args.limit_items,
        collection_limit_items=args.collection_limit_items,
        release_collection_limit=args.release_collection_limit,
        workflow_collection_limit=args.workflow_collection_limit,
        include_bots=args.include_bots,
        include_external_activity=args.include_external_activity,
    )


def evidence_payload(payload: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    ignored_keys = settings.get("evidence_ignored_config_keys") or []
    source_notes = list(payload.get("limitations") or [])
    if ignored_keys:
        source_notes.append(
            "Ignored audience/report-rendering config keys for evidence-only output: " + ", ".join(ignored_keys) + "."
        )
    evidence = {
        "ok": bool(payload.get("ok")),
        "schema_version": SCHEMA_VERSION,
        "kind": "github_work_evidence",
        "generated_at": payload.get("generated_at"),
        "window": payload.get("window"),
        "display_window": payload.get("display_window"),
        "timezone": payload.get("timezone"),
        "scope": {
            "repositories": payload.get("repositories") or [],
            "subjects": payload.get("subjects") or [],
            "mode": payload.get("mode"),
            "collection_lanes": payload.get("collection_lanes") or {},
        },
        "preflight": payload.get("preflight") or {},
        "summary": payload.get("summary") or {},
        "buckets": payload.get("buckets") or {},
        "priority_sections": payload.get("priority_sections") or [],
        "releases": payload.get("releases") or [],
        "workflows": payload.get("workflows") or [],
        "source_notes": source_notes,
    }
    return strip_non_evidence_fields(rollup.sanitize_payload_for_json(evidence))


def strip_non_evidence_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip_non_evidence_fields(item) for key, item in value.items() if key != "handoff"}
    if isinstance(value, list):
        return [strip_non_evidence_fields(item) for item in value]
    return value


def evidence_failure(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "kind": "github_work_evidence",
        "generated_at": payload.get("generated_at"),
        "window": payload.get("window"),
        "display_window": payload.get("display_window"),
        "timezone": payload.get("timezone"),
        "error": payload.get("error"),
        "next_step": payload.get("next_step"),
        "source_notes": ["Evidence collection failed before a complete source snapshot could be built."],
    }


def write_or_print_json(payload: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    rollup.write_or_print(rendered, output)


if __name__ == "__main__":
    raise SystemExit(main())
