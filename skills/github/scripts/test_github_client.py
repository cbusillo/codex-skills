#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Client resolution tests use fake helper records and private overlay fixtures."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import github_client as client

REPO = "example/product"


def test_launchplane_record_wins_and_readable_empty_does_not_fallback() -> None:
    mapping = {"status": "available", "sections": {"repo_product_mapping": {
        "status": "available", "repositories": [{"repository": REPO, "product_key": "product"}]}}}
    profile = {"status": "ok", "result": {"product": "product", "repository": REPO, "owner_github_login": "Client"}}
    with patch.object(client, "overlay_client", side_effect=AssertionError("unexpected fallback")):
        with patch.object(client, "helper_json", side_effect=[mapping, profile]):
            assert client.recorded_client(REPO) == {"status": "recorded", "source": "launchplane", "login": "client"}
        empty = {**profile, "result": {**profile["result"], "owner_github_login": ""}}
        with patch.object(client, "helper_json", side_effect=[mapping, empty]):
            assert client.recorded_client(REPO)["status"] == "none"
        other = {**profile, "result": {**profile["result"], "repository": "example/other"}}
        with patch.object(client, "helper_json", side_effect=[mapping, other]):
            assert client.recorded_client(REPO)["status"] == "ambiguous"
        with patch.object(client, "helper_json", return_value={"status": "available", "sections": {
                "repo_product_mapping": {"status": "available", "repositories": []}}}):
            assert client.recorded_client(REPO)["status"] == "none"
    with patch.object(client, "helper_json", side_effect=[mapping, None]), patch.object(client, "local_repository", return_value=None):
        assert client.recorded_client(REPO)["status"] == "unavailable"


def test_private_overlay_requires_one_explicit_repository_client() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        assert client.overlay_client(root)["status"] == "unavailable"
        overlay = root / ".local/people.yaml"
        overlay.parent.mkdir()
        overlay.write_text("version: 1\npeople:\n  - id: staff\n    display_name: Staff\n    relationship: {kind: collaborator}\n    contacts: {github: {username: staff}}\n")
        assert client.overlay_client(root)["status"] == "none"
        overlay.write_text(overlay.read_text() + "  - id: client\n    display_name: Client\n    relationship: {kind: client}\n    contacts: {github: {username: client, bot_usernames: [client-bot]}}\n")
        with patch.object(client, "helper_json", return_value=None), patch.object(client, "local_repository", return_value=root):
            record = client.recorded_client(REPO)
        assert record == {"status": "recorded", "source": "repo_people_overlay", "login": "client"}
        assert client.is_client_issue({"user": {"login": "CLIENT"}}, record)
        assert not client.is_client_issue({"user": {"login": "client-bot"}}, record)
        overlay.write_text(overlay.read_text() + "  - id: another\n    display_name: Another\n    relationship: {roles: [client]}\n    contacts: {github: {username: another}}\n")
        assert client.overlay_client(root)["status"] == "ambiguous"
        overlay.write_text("not: [valid")
        assert client.overlay_client(root)["status"] == "unavailable"
        overlay.write_bytes(b"\xff")
        assert client.overlay_client(root)["status"] == "unavailable"


def test_local_overlay_is_read_only_after_origin_matches() -> None:
    from types import SimpleNamespace
    def git(*args: object, **_kwargs: object) -> SimpleNamespace:
        command = args[0]
        if "rev-parse" in command:
            return SimpleNamespace(stdout="/checkouts/current/.git\n", returncode=0)
        return SimpleNamespace(stdout="git@github.com:someone-else/product.git\n", returncode=0)
    with patch.object(client.subprocess, "run", side_effect=git):
        assert client.local_repository(REPO) is None


def test_matching_primary_and_sibling_origins_select_only_that_repository() -> None:
    from types import SimpleNamespace
    def git(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if "rev-parse" in command:
            return SimpleNamespace(stdout="/checkouts/current/.git\n", returncode=0)
        repo = "example/current" if command[2] == "/checkouts/current" else REPO
        return SimpleNamespace(stdout=f"git@github.com:{repo}.git\n", returncode=0)
    with patch.object(client.subprocess, "run", side_effect=git):
        assert client.local_repository("example/current") == Path("/checkouts/current")
        assert client.local_repository(REPO) == Path("/checkouts/product")


def test_optional_people_helper_absence_does_not_stop_selection() -> None:
    import builtins
    original = builtins.__import__
    def without_people(name: str, *args: object, **kwargs: object) -> object:
        if name == "skills.people.scripts.resolve_person":
            raise ImportError("Optional people skill not installed")
        return original(name, *args, **kwargs)
    with patch("builtins.__import__", side_effect=without_people):
        assert client.overlay_client(Path("/fixture/repo"))["status"] == "unavailable"


def main() -> None:
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"ok {name}")


if __name__ == "__main__":
    main()
