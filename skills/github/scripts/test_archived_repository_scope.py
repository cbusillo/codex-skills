#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Archive fixture proves search results, counts and discovery scope."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

SCRIPTS = Path(__file__).resolve().parent
SKILLS = SCRIPTS.parents[1]
for directory in (SCRIPTS, SKILLS / "direction/scripts", SKILLS / "github-work-rollup/scripts"):
    sys.path.insert(0, str(directory))
import director_waits
import direction_audit
import github_work_rollup as rollup
import github_unanswered_comments as attention
from test_gh_with_env_token import run_wrapper, user_token_fixture, write

SPEC = importlib.util.spec_from_file_location("archive_plan", SCRIPTS / "gh-plan.py")
assert SPEC is not None and SPEC.loader is not None
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)
REPOS = json.loads((SCRIPTS / "fixtures/archived-repositories.json").read_text())
NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


def search_items(query):
    # Fake provider honors the archive qualifier against real repository state.
    return [{"number": repo["number"], "state": "open", "title": "Wait",
             "repository_url": f'https://api.github.com/repos/{repo["full_name"]}',
             "html_url": f'https://github.com/{repo["full_name"]}/issues/{repo["number"]}',
             "body": "## Current Status\nWaiting for: Director to approve"}
            for repo in REPOS if not (repo["archived"] and "archived:false" in query.split())
            and not (not repo["archived"] and "archived:true" in query.split())]


class ArchiveTests(unittest.TestCase):
    def test_helper_opt_in_survives_the_real_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_path:
            root = Path(temporary_path)
            env_file, classifier, identity, fake_gh = user_token_fixture(root)
            write(fake_gh, f'''#!{sys.executable}
import json, sys
from urllib.parse import parse_qs, urlsplit
args = sys.argv[1:]
query = " ".join(args)
for arg in args:
    query += " " + " ".join(parse_qs(urlsplit(arg).query).get("q", []))
rows = json.loads({json.dumps(search_items(''))!r})
repositories = json.loads({json.dumps(REPOS)!r})
archived = {{repo["full_name"] for repo in repositories if repo["archived"]}}
if "archived:false" in query:
    rows = [row for row in rows if "/".join(row["repository_url"].split("/")[-2:]) not in archived]
print(json.dumps({{"items": rows, "total_count": len(rows), "incomplete_results": False}}))
''')

            def delegate(command: list[str]) -> subprocess.CompletedProcess[str]:
                reply = run_wrapper(env_file, classifier, identity, *command[1:], gh_command=fake_gh)
                self.assertEqual(reply.returncode, 0, reply.stderr)
                return reply

            for included in (False, True):
                expected = len(REPOS) if included else sum(not repo["archived"] for repo in REPOS)
                result = subprocess.run(
                    [sys.executable, str(SCRIPTS / "gh-plan.py"), "search", "user:owner", *(["--include-archived"] if included else [])],
                    cwd=root, text=True, capture_output=True, timeout=20,
                    env={"PATH": os.environ["PATH"], "HOME": str(root), "CODEX_SKILLS_ENV_FILE": str(env_file),
                         "GH_WITH_ENV_TOKEN_CLASSIFIER": str(classifier), "GH_WITH_ENV_TOKEN_IDENTITY_HELPER": str(identity),
                         "GH_WITH_ENV_TOKEN_PYTHON": sys.executable, "GH_WITH_ENV_TOKEN_GH": str(fake_gh)},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["total_count"], expected)
                settings = {"subjects": ["owner"], "window": rollup.Window(NOW, NOW, "today"), "include_bots": True, "include_archived": included}
                with patch_attr(rollup, "run", delegate), patch_attr(rollup, "subject_item_in_window", return_value=True):
                    self.assertEqual(len(rollup.collect_subject_items(settings)), expected)

    def test_plan_search_count_excludes_archived_repository(self):
        def read(path, **kwargs):
            rows = search_items(kwargs["query"]["q"])
            kwargs["page_metadata"].append({"total_count": len(rows), "incomplete_results": False})
            return "bot", rows
        args = PLAN.build_parser().parse_args(["search", "user:owner"])
        with patch.object(PLAN, "collect_paged_rest_items", read), patch.object(PLAN, "repo_from_git", return_value=None), patch.object(PLAN, "emit") as emit:
            PLAN.cmd_search(args)
        self.assertEqual(emit.call_args.args[0]["count"], 1)
        self.assertEqual(emit.call_args.args[0]["total_count"], 1)

    def test_archive_query_qualifier_cannot_bypass_default_and_opt_in_preserves_it(self):
        import github_repository_scope as scope
        query = 'user:owner archived:true "archived:false"'
        self.assertEqual([row["number"] for row in search_items(scope.search_query(query))], [REPOS[0]["number"]])
        self.assertEqual([row["number"] for row in search_items(scope.search_query(query, include_archived=True))], [REPOS[1]["number"]])
        self.assertIn('"archived:false"', scope.search_query(query))

    def test_director_wait_count_excludes_archived_repository(self):
        class Reader:
            def request(self, method, path, **kwargs):
                rows = search_items(parse_qs(urlsplit(path).query)["q"][0])
                return SimpleNamespace(body={"items": rows, "total_count": len(rows), "incomplete_results": False})

            def paged_json(self, path, **kwargs):
                return []
        result = director_waits.collect(Reader(), "owner", ["Director"], NOW)
        self.assertEqual([row["repo"] for row in result["waits"]], ["owner/current"])
        self.assertTrue(result["complete"])

    def test_subject_rollup_excludes_archived_repository(self):
        def run(command):
            query = next(field[2:] for field in command if field.startswith("q="))
            rows = search_items(query)
            return subprocess.CompletedProcess(command, 0, json.dumps({"items": rows, "total_count": len(rows)}), "")
        settings = {"subjects": ["owner"], "window": rollup.Window(NOW, NOW, "today"), "include_bots": True}
        with patch.object(rollup, "run", run), patch.object(rollup, "subject_item_in_window", return_value=True):
            rows = rollup.collect_subject_items(settings)
        self.assertEqual(len(rows), 1)

    def test_archived_only_rollup_names_the_opt_in_and_mixed_scope_warns(self):
        settings = {"repositories": ["owner/retired"], "repo_owners": [], "subjects": []}
        with patch.object(rollup, "github_preflight", return_value={}), patch.object(rollup, "run", return_value=subprocess.CompletedProcess([], 0, '{"isArchived": true}', "")):
            with self.assertRaisesRegex(rollup.RollupError, "include-archived"):
                rollup.collect_rollup(settings)
        self.assertTrue(any("owner/retired" in warning for warning in settings["collection_warnings"]))

    def test_direction_repository_discovery_excludes_archived(self):
        repos, cut = direction_audit.owner_repositories("owner", fetch=lambda command: {"repositories": REPOS})
        self.assertFalse(cut)
        self.assertEqual(list(repos), ["owner/current"])


    def test_search_and_counts_include_archived_with_cli_opt_in(self):
        args = PLAN.build_parser().parse_args(["search", "user:owner", "--include-archived"])
        def read(path, **kwargs):
            rows = search_items(kwargs["query"]["q"])
            kwargs["page_metadata"].append({"total_count": len(rows), "incomplete_results": False})
            return "bot", rows
        with patch.object(PLAN, "collect_paged_rest_items", read), patch.object(PLAN, "repo_from_git", return_value=None), patch.object(PLAN, "emit") as emit:
            PLAN.cmd_search(args)
        self.assertEqual(emit.call_args.args[0]["count"], len(REPOS))
        self.assertEqual(emit.call_args.args[0]["total_count"], len(REPOS))

    def test_director_waits_opt_in_restores_archived_waits(self):
        class Reader:
            def request(self, method, path, **kwargs):
                rows = search_items(parse_qs(urlsplit(path).query)["q"][0])
                return SimpleNamespace(body={"items": rows, "total_count": len(rows), "incomplete_results": False})

            def paged_json(self, path, **kwargs):
                return []
        report = director_waits.collect(Reader(), "owner", ["Director"], NOW, include_archived=True)
        self.assertEqual(len(report["waits"]), len(REPOS))
        self.assertTrue(report["complete"])

    def test_rollup_opt_in_restores_archived_subject_activity(self):
        def run(command):
            query = next(field[2:] for field in command if field.startswith("q="))
            rows = search_items(query)
            return subprocess.CompletedProcess(command, 0, json.dumps({"items": rows, "total_count": len(rows)}), "")
        settings = {"subjects": ["owner"], "window": rollup.Window(NOW, NOW, "today"), "include_bots": True, "include_archived": True}
        with patch.object(rollup, "run", run), patch.object(rollup, "subject_item_in_window", return_value=True):
            self.assertEqual(len(rollup.collect_subject_items(settings)), len(REPOS))

    def test_direction_discovery_opt_in_restores_archived_repositories(self):
        repos, cut = direction_audit.owner_repositories("owner", fetch=lambda command: {"repositories": REPOS}, include_archived=True)
        self.assertEqual(len(repos), len(REPOS))
        self.assertFalse(cut)

    def test_attention_configured_archives_do_not_fall_back_to_checkout(self):
        settings = {"repositories": ["owner/retired"], "repo_owners": [], "threads": [], "explicit_scope": False}
        with patch.object(attention, "run_json", return_value={"isArchived": True}), patch.object(attention, "current_repository") as current:
            with self.assertRaises(rollup.RollupError):
                attention.resolve_repositories(settings)
        current.assert_not_called()
        self.assertEqual(settings["excluded_archived_repositories"], ["owner/retired"])

    def test_plan_index_archived_repository_is_opt_in(self):
        for included in (False, True):
            with self.subTest(included=included):
                args = PLAN.build_parser().parse_args(["--repo", "owner/retired", "index", *(["--include-archived"] if included else [])])
                with patch.object(PLAN, "load_config", return_value={"labels": {"plan": "plan"}}), patch.object(PLAN, "collect_paged_rest_items", return_value=("bot", search_items(""))), patch.object(PLAN, "api_json", return_value=("bot", REPOS[1])), patch.object(PLAN, "emit") as emit:
                    PLAN.cmd_index(args)
                self.assertEqual(emit.call_args.args[0]["count"], len(REPOS) if included else 0)
                self.assertEqual(emit.call_args.args[0]["repository"]["disposition"], "frozen_historical")

    def test_global_next_discovery_archive_scope(self):
        def read(path, **kwargs):
            if path == "/installation/repositories":
                return "bot", REPOS
            repo = "/".join(path.split("/")[2:4])
            return "bot", [row for row in search_items("") if row["repository_url"].endswith(repo)]
        for included in (False, True):
            with self.subTest(included=included):
                args = PLAN.build_parser().parse_args(["next", *(["--include-archived"] if included else [])])
                with patch.object(PLAN.github_identity, "github_app_config", return_value={}), patch.object(PLAN, "collect_paged_rest_items", read), patch.object(PLAN, "load_direction", return_value=None):
                    issues, coverage = PLAN.discover_direction_work("owner/direction", args, selection_context={})
                self.assertEqual(len(issues), len(REPOS) if included else 1)
                self.assertTrue(coverage["complete"])

    def test_rollup_and_attention_repository_inventory_archive_scope(self):
        listing = [{"nameWithOwner": repo["full_name"], "isArchived": repo["archived"]} for repo in REPOS]
        def payload(command):
            if command[1:3] == ["repo", "list"]:
                return listing
            return next(repo for repo in listing if repo["nameWithOwner"] == command[3])
        def run(command):
            return subprocess.CompletedProcess(command, 0, json.dumps(payload(command)), "")
        for included in (False, True):
            for explicit in (False, True):
                with self.subTest(included=included, explicit=explicit):
                    settings = {"repositories": [repo["full_name"] for repo in REPOS] if explicit else [], "repo_owners": [] if explicit else ["owner"], "threads": [], "limit_repos": 10, "include_archived": included, "explicit_scope": True}
                    with patch.object(rollup, "run", run), patch.object(attention, "run_json", payload):
                        for resolve in (rollup.resolve_repositories, attention.resolve_repositories):
                            self.assertEqual(resolve(settings), [repo["full_name"] for repo in REPOS if included or not repo["archived"]])


if __name__ == "__main__":
    unittest.main()
