#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SHORTCUT_NAME = "Agent Reminder Link"
INPUT_KEYS = ("title", "notes", "url")
REMINDER_PARAMETERS = {"title": "WFCalendarItemTitle", "notes": "WFCalendarItemNotes", "url": "WFURL"}


def action(identifier: str, **parameters) -> dict:
    return {"WFWorkflowActionIdentifier": identifier, "WFWorkflowActionParameters": parameters}


def output(source: str, name: str) -> dict:
    return {"OutputUUID": source, "OutputName": name, "Type": "ActionOutput"}


def definition() -> dict:
    dictionary = str(uuid.uuid4()).upper()
    values = {key: str(uuid.uuid4()).upper() for key in INPUT_KEYS}
    actions = [action("is.workflow.actions.detect.dictionary", UUID=dictionary, WFInput={
        "Value": {"Type": "ExtensionInput"}, "WFSerializationType": "WFTextTokenAttachment"})]
    actions += [action("is.workflow.actions.getvalueforkey", UUID=values[key], WFDictionaryKey=key, WFInput={
        "Value": output(dictionary, "Dictionary"), "WFSerializationType": "WFTextTokenAttachment"})
        for key in INPUT_KEYS]
    actions.append(action("is.workflow.actions.addnewreminder", UUID=str(uuid.uuid4()).upper(), **{
        REMINDER_PARAMETERS[key]: {"Value": {"string": "￼", "attachmentsByRange": {
            "{0, 1}": output(values[key], "Dictionary Value")}}, "WFSerializationType": "WFTextTokenString"}
        for key in INPUT_KEYS}))
    return {"WFWorkflowClientVersion": "3000", "WFWorkflowMinimumClientVersion": 900,
            "WFWorkflowMinimumClientVersionString": "900",
            "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
            "WFWorkflowInputContentItemClasses": ["WFGenericFileContentItem", "WFDictionaryContentItem"],
            "WFWorkflowHasShortcutInputVariables": True, "WFWorkflowTypes": [],
            "WFWorkflowImportQuestions": [], "WFWorkflowOutputContentItemClasses": [],
            "WFWorkflowActions": actions}


def installed() -> bool:
    names = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, check=True).stdout
    return SHORTCUT_NAME in names.splitlines()


def install(folder: Path) -> Path:
    unsigned = folder / f"{SHORTCUT_NAME}.unsigned.shortcut"
    signed = folder / f"{SHORTCUT_NAME}.shortcut"
    unsigned.write_bytes(plistlib.dumps(definition(), fmt=plistlib.FMT_BINARY))
    subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", str(unsigned), "--output", str(signed)],
                   check=True, capture_output=True, text=True)
    subprocess.run(["open", str(signed)], check=True)
    return signed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the shortcut that gives agent reminders a visible, clickable URL")
    parser.add_argument("--check", action="store_true", help="Report whether the shortcut is installed")
    args = parser.parse_args()
    if sys.platform != "darwin":
        print("error: the reminder link shortcut requires macOS Shortcuts", file=sys.stderr)
        return 1
    try:
        if installed():
            print(f"{SHORTCUT_NAME} is installed")
            return 0
        if args.check:
            print(f"{SHORTCUT_NAME} is not installed; run this script without --check to install it")
            return 1
        signed = install(Path(tempfile.mkdtemp(prefix="reminder-shortcut-")))
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {getattr(exc, 'stderr', '') or exc}", file=sys.stderr)
        return 1
    print(f"Opened {signed}. Click Add Shortcut in Shortcuts, keeping the name {SHORTCUT_NAME}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
