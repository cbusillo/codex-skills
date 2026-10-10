#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared syntax hint for Launchplane product-review records, never authority."""

import re

PRODUCT_REVIEW_MARKER = re.compile(r"<!-- launchplane:product-review:([A-Za-z0-9_.:-]+) -->")
