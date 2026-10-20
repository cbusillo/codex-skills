#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Bounded, line-framed observations of this run's native engine output."""
import re


class ObservationError(Exception):
    pass


class LineFramer:
    def __init__(self, limit=65536):
        self.pending = b""
        self.limit = limit

    def feed(self, data, *, final=False):
        pieces = (self.pending + data).split(b"\n")
        self.pending = pieces.pop()
        if any(len(line) > self.limit for line in [*pieces, self.pending]):
            raise ObservationError("native_output_line_over_limit")
        if final and self.pending:
            pieces.append(self.pending)
            self.pending = b""
        return [line.decode(errors="replace") for line in pieces], self.pending.decode(errors="replace")


LOG_EVENT = re.compile(r"^\d{4}-\d\d-\d\d\s+[\d:, .]+\[\s*\d+\]\s+\w+\s+-\s+\S+\s+-\s+(.*)$")
LICENSE_PROMPT = re.compile(
    r"^(?:no valid licen[cs]e\b|checking trial availability\b|"
    r"(?:start|activate|try|begin|offer) (?:a |your |free )?trial\b|"
    r"(?:your |the |a )?(?:trial|evaluation) (?:is |has )?(?:expired|period|available|active|licen[cs]e|expires|ends)\b|"
    r"you are using (?:a )?trial\b|(?:your |the |this |a )?licen[cs]e.{0,40}(?:expired|invalid|missing|required)|"
    r"(?:activate|purchase|buy|log.?in).{0,40}licen[cs]e|third.party privacy notice.*not accepted)", re.I)


def license_boundary(text):
    for line in text.splitlines():
        logged = LOG_EVENT.match(line)
        event = logged.group(1) if logged else line
        if LICENSE_PROMPT.search(event.strip()):
            return True
    return False


class EngineObserver:
    def __init__(self, project, receipt):
        self.project = project
        self.receipt = receipt
        self.completion = False

    def observe(self, line, *, partial=False):
        logged = LOG_EVENT.match(line)
        event = logged.group(1) if logged else line
        if event.startswith(("Running local inspections on ", "Inspecting ")):
            if not partial and event.startswith("Running local inspections on "):
                from pathlib import Path
                source = Path(event.removeprefix("Running local inspections on ").strip())
                if source.is_absolute() and source.is_relative_to(self.project):
                    relative = source.relative_to(self.project).as_posix()
                    if relative not in self.receipt.setdefault("inspected_files", []):
                        self.receipt["inspected_files"].append(relative)
            return
        if license_boundary(line):
            self.receipt["license_boundary_observed"] = True
            raise ObservationError("native_owner_activation_required")
        plugin = re.search(r"Odoo IDE \(([0-9][\w.+-]*)\)", line)
        if plugin:
            self.receipt["odoo_plugin_version"] = plugin.group(1)
        if not partial and re.fullmatch(r"Done\.?\s*", line):
            self.completion = True
        if re.search(r"Using default project profile|Cannot load project|No files to inspect|No modules", line, re.I):
            raise ObservationError("native_project_or_profile_unprepared")
        if re.search(r"^ERROR:|\sERROR\s+-", line):
            category = re.search(r"\bERROR\s+-\s+([#A-Za-z0-9_.-]{1,128})\s+-", line)
            self.receipt["engine_error_category"] = category.group(1) if category else "stdout_or_unclassified"
            raise ObservationError("native_engine_error")
        if re.search(r"Cancelling inspection progress|\b(?:ProcessCanceledException|CancellationException)\b", line):
            raise ObservationError("native_execution_cancelled")
