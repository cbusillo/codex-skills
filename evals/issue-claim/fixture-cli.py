#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read-only simulation for native CLI ordering trials; no real GitHub/Git writes."""

import json
import sys

argv = sys.argv[1:]
if not argv:
    raise SystemExit("Pass the normal operation arguments after fixture-cli.py")
if "show" in argv or "next" in argv:
    print(json.dumps({"ok": True, "issue": {"number": 42, "state": "open", "title": "Repair the fixture",
                                             "body": "## Current Status\nState: Active, not started.\nNext action: implement.\nBlocked by: None.", "comments": []}}))
elif "claim" in argv and any("gh-plan.py" in part for part in argv):
    required = ["--worker", "--session", "--branch", "--next-action"]
    if any(flag not in argv or argv.index(flag) + 1 >= len(argv) for flag in required):
        print(json.dumps({"ok": False, "error": "claim fields missing"}))
        raise SystemExit(2)
    print(json.dumps({"ok": True, "outcome_certainty": "confirmed", "exclusive_lock": False,
                      "completed_steps": ["ownership_preflight", "post_claim", "claim_readback",
                                          "update_current_status", "update_labels", "metadata_readback"],
                      "session_coverage": {"codex": {"status": "unavailable"}}, "simulation": True}))
elif "worktree" in argv or any("dev-worktree" in part for part in argv):
    print(json.dumps({"ok": True, "simulation": True, "operation": "worktree_create"}))
elif "status" in argv or "branch" in argv or "remote" in argv:
    print(json.dumps({"ok": True, "branch": "main", "default_branch": "main", "simulation": True}))
else:
    print(json.dumps({"ok": False, "error": "unsupported simulated operation", "argv": argv}))
    raise SystemExit(2)
