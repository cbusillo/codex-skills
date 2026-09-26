#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read one scoped Owner review, including private prose, from Launchplane."""

import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
import urllib.error

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location(
    "launchplane_write_action", SCRIPT_DIR / "launchplane-write-action.py"
)
assert spec is not None and spec.loader is not None
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)

DECISION_FIELDS = (
    "record_id",
    "product",
    "repository",
    "pull_request_number",
    "head_sha",
    "preview_url",
    "decision",
    "reason",
    "owner_github_id",
    "owner_github_login",
    "decided_at",
    "feedback_url",
)


def read_review(args):
    settings = operator.resolve_settings(args)
    operator.validate_service_url(settings["service_url"])
    if not settings["token"]:
        raise ValueError("missing_operator_token")
    query = {"repository": args.repo, "pull_request": str(args.pr)}
    if args.decision_id:
        query["decision_id"] = args.decision_id
    # Bounded local extension of the existing private product-review read. The
    # public-safe context/write helper intentionally never emits Owner prose.
    payload = operator.request_launchplane_read(
        service_url=settings["service_url"],
        path="/v1/product-review",
        settings=settings,
        query=query,
        timeout=args.timeout,
    )
    if (
        payload.get("status") != "ok"
        or str(payload.get("repository") or "").casefold() != args.repo.casefold()
        or type(payload.get("pull_request_number")) is not int
        or payload["pull_request_number"] != args.pr
    ):
        raise ValueError("unexpected_review_subject")
    decision = payload.get("latest_decision")
    if decision is None and not args.decision_id:
        return None
    if (
        not isinstance(decision, dict)
        or any(key not in decision for key in DECISION_FIELDS)
        or str(decision.get("repository") or "").casefold() != args.repo.casefold()
        or type(decision.get("pull_request_number")) is not int
        or decision["pull_request_number"] != args.pr
        or (args.decision_id and decision.get("record_id") != args.decision_id)
    ):
        raise ValueError("unexpected_review_decision")
    return {key: decision[key] for key in DECISION_FIELDS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--decision-id", default="")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", args.repo) or args.pr < 1 or args.timeout <= 0:
        parser.error("a valid repository, positive PR number, and positive timeout are required")
    try:
        decision = read_review(args)
    except urllib.error.HTTPError as error:
        print(
            json.dumps(
                {"ok": False, "error": "owner_review_read_failed", "status_code": error.code}
            )
        )
        return 1
    except (ValueError, OSError, TimeoutError, urllib.error.URLError):
        # Never print config, credentials, provider bodies, or exception text.
        print(json.dumps({"ok": False, "error": "owner_review_read_unavailable"}))
        return 1
    print(json.dumps({"ok": True, "decision": decision}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
