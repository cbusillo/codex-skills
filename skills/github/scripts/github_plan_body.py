# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Compatibility exports for the canonical planning ownership envelope."""
try:
    from github_plan_status.body import (
        PlanBodyError as PlanBodyError,
        PLAN_ORIGINAL_START as PLAN_ORIGINAL_START,
        PLAN_ORIGINAL_END as PLAN_ORIGINAL_END,
        PLAN_MANAGED_START as PLAN_MANAGED_START,
        PLAN_MANAGED_END as PLAN_MANAGED_END,
        PLAN_MANAGED_PROVENANCE_MARKER as PLAN_MANAGED_PROVENANCE_MARKER,
        PLAN_OWNERSHIP_MARKERS as PLAN_OWNERSHIP_MARKERS,
        PLAN_RESERVED_MARKERS as PLAN_RESERVED_MARKERS,
        PLAN_MANAGED_NOTICE as PLAN_MANAGED_NOTICE,
        marked_block as marked_block,
        contributor_plan_body as contributor_plan_body,
        wrap_contributor_plan as wrap_contributor_plan)
except ModuleNotFoundError:
    from skills.github.scripts.github_plan_status.body import (
        PlanBodyError as PlanBodyError,
        PLAN_ORIGINAL_START as PLAN_ORIGINAL_START,
        PLAN_ORIGINAL_END as PLAN_ORIGINAL_END,
        PLAN_MANAGED_START as PLAN_MANAGED_START,
        PLAN_MANAGED_END as PLAN_MANAGED_END,
        PLAN_MANAGED_PROVENANCE_MARKER as PLAN_MANAGED_PROVENANCE_MARKER,
        PLAN_OWNERSHIP_MARKERS as PLAN_OWNERSHIP_MARKERS,
        PLAN_RESERVED_MARKERS as PLAN_RESERVED_MARKERS,
        PLAN_MANAGED_NOTICE as PLAN_MANAGED_NOTICE,
        marked_block as marked_block,
        contributor_plan_body as contributor_plan_body,
        wrap_contributor_plan as wrap_contributor_plan)
