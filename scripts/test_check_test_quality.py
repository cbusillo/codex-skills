#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise the five scan rules on synthetic programs, never real workflow text."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import check_test_quality as guard


class Patterns(unittest.TestCase):
    def rules(self, source: str, *, paths: set[str] | None = None,
              versions: set[str] | None = None, catalog: bool = False,
              suffix: str = ".py") -> set[str]:
        parser = guard.python_findings if suffix == ".py" else guard.foreign_findings
        return {item.rule for item in parser("tests/test_example" + suffix, source,
                                            paths or set(), versions or set(), catalog)}

    def test_bd_release_config_value_and_uv_specifier(self) -> None:
        source = '''
REPO_ROOT = Path(__file__).resolve().parents[1]
def test_release():
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    ASSERTION
    app = pyproject["tool"]["release"]
'''
        for assertion in (
            'self.assertEqual(pyproject["tool"]["uv"]["required-version"], ">=0.11.31")',
            'self.assertEqual(pyproject["tool"]["release"]["bundle_identifier"], "com.example.app")',
        ):
            with self.subTest(assertion=assertion):
                self.assertIn("pinned-config", self.rules(source.replace("ASSERTION", assertion), paths={"pyproject.toml"}))

    def test_workflow_loader_alias_and_action_pin(self) -> None:
        source = '''
ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "retention.yml"
def load_workflow(path):
    with path.open() as stream:
        return yaml.safe_load(stream)
def test_retention():
    workflow = load_workflow(WORKFLOW)
    steps = workflow["jobs"]["cleanup"]["steps"]
    self.assertEqual(steps[0]["uses"], "docker/login-action@v4")
'''
        result = self.rules(source, paths={".github/workflows/retention.yml"}, versions={"@v4"})
        self.assertTrue({"pinned-config", "workflow-no-execution", "duplicated-version"} <= result)

    def test_source_text_with_tokens_and_inspect_getsource(self) -> None:
        source = '''
def test_rollback():
    rollback_source = Path("control_plane/rollback.py").read_text()
    required_tokens = ("execute_stable_apply", "StableApplyRequest")
    for token in required_tokens:
        self.assertIn(token, rollback_source)
'''
        self.assertIn("source-text", self.rules(source, paths={"control_plane/rollback.py"}))
        self.assertIn("source-text", self.rules('self.assertNotIn("direct_provider_mutation", inspect.getsource(rollback))'))

    def test_homekit_version_only_when_duplicated(self) -> None:
        source = 'self.assertEqual("2026.8.3", plan["compatibility"]["source_tag"])'
        self.assertIn("duplicated-version", self.rules(source, versions={"2026.8.3"}))
        self.assertEqual(self.rules(source), set())

    def test_catalog_instruction_rule_is_catalog_only(self) -> None:
        source = 'text = (ROOT / "skills/references/guide.md").read_text()\nself.assertIn("Do not run the marking helpers yourself", text)'
        paths = {"skills/references/guide.md"}
        self.assertIn("instruction-text", self.rules(source, paths=paths, catalog=True))
        self.assertEqual(self.rules(source, paths=paths), set())
        source = 'text = (ROOT / "instructions" / "global.md").read_text()\nself.assertIn("Keep a test only if", text)'
        self.assertIn("instruction-text", self.rules(source, paths={"instructions/global.md"}, catalog=True))

    def test_swift_team_id_and_plist_literal(self) -> None:
        entitlements = '''
let entitlements = try loadEntitlements("Config/App.entitlements")
let groups = try #require(entitlements["keychain-access-groups"] as? [String])
#expect(groups == ["EXAMPLETEAM.com.example.provider-credentials"])
'''
        plist = '''
let plist = try loadInfoPlist("Config/App-Info.plist")
#expect(plist["CFBundleURLTypes"] == ["example"])
'''
        for source in (entitlements, plist):
            with self.subTest(source=source):
                self.assertIn("pinned-config", self.rules(source, suffix=".swift",
                              paths={"Config/App.entitlements", "Config/App-Info.plist"}))

    def test_javascript_workflow_and_duplicated_action(self) -> None:
        source = 'const workflow = readFileSync(".github/workflows/ci.yml", "utf8");\nexpect(workflow).toEqual("docker/login-action@v4");'
        result = self.rules(source, suffix=".ts", paths={".github/workflows/ci.yml"}, versions={"@v4"})
        self.assertIn("duplicated-version", result)
        self.assertIn("workflow-no-execution", result)
        self.assertIn("pinned-config", result)

    def test_javascript_import_require_and_node_assertions(self) -> None:
        for source in (
            'const pkg = require("../package.json"); expect(pkg.name).toBe("app");',
            'import pkg from "./package.json"; expect(pkg.name).not.toEqual("old");',
            'const pkg = require("./package.json"); assert.strictEqual(pkg.name, "app");',
            'const pkg = require("./package.json"); expect(pkg.name).toStrictEqual("app");',
        ):
            self.assertIn("pinned-config", self.rules(source, suffix=".ts", paths={"package.json"}))
        source = 'expect(output).toContain("actions/checkout@v4");'
        self.assertIn("duplicated-version", self.rules(source, suffix=".ts", versions={"@v4"}))

    def test_path_arguments_and_async_class_methods(self) -> None:
        source = '''
class Tests(IsolatedAsyncioTestCase):
    async def test_workflow(self):
        config = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertEqual(config, "literal")
'''
        self.assertTrue({"pinned-config", "workflow-no-execution"} <= self.rules(
            source, paths={".github/workflows/ci.yml"}))
        source = 'with (ROOT / "skills" / "references" / "guide.md").open("r", encoding="utf-8") as file:\n text = file.read()\nself.assertIn("wording", text)'
        self.assertIn("instruction-text", self.rules(source, paths={"skills/references/guide.md"}, catalog=True))

    def test_tracked_config_fixtures_and_product_execution_are_behavior(self) -> None:
        source = 'data = json.loads(Path("tests/fixtures/package.json").read_text())\nself.assertEqual(analyze(data).name, "fixture-app")'
        self.assertEqual(self.rules(source, paths={"tests/fixtures/package.json"}), set())
        source = 'data = json.loads(Path("package.json").read_text())\nself.assertEqual(analyze(data).name, "fixture-app")'
        self.assertEqual(self.rules(source, paths={"package.json"}), set())
        source = 'FIXTURES = ROOT / "tests" / "fixtures"\nconfig = load_config(FIXTURES / "minimal" / "pyproject.toml")\nself.assertEqual(config.name, "minimal")'
        paths = {"pyproject.toml", "tests/fixtures/minimal/pyproject.toml"}
        self.assertEqual(self.rules(source, paths=paths), set())
        source = 'config = json.loads((tmp_path_factory.mktemp("repo") / "package.json").read_text())\nself.assertEqual(config["name"], "minimal")'
        self.assertEqual(self.rules(source, paths={"package.json"}), set())

    def test_workflow_execution_keeps_behavior_tests(self) -> None:
        source = '''
workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
result = subprocess.run(["bash", "-c", workflow["jobs"]["build"]["steps"][0]["run"]])
self.assertEqual(result.returncode, 0)
self.assertIn("The requested operation could not be completed.", result.stderr)
'''
        self.assertEqual(self.rules(source, paths={".github/workflows/ci.yml"}), set())

    def test_workflow_step_run_by_class_helper_method_is_execution(self) -> None:
        source = '''
class Tests(unittest.TestCase):
    def setUp(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        self.shell = workflow["jobs"]["build"]["steps"][0]["run"]
    def run_step(self):
        return subprocess.run(["bash", "-c", self.shell])
    def test_step(self):
        self.assertEqual(self.run_step().returncode, 0)
'''
        paths = {".github/workflows/ci.yml"}
        self.assertEqual(self.rules(source, paths=paths), set())
        lint = source.replace('subprocess.run(["bash", "-c", self.shell])', "self.shell.split()")
        self.assertIn("workflow-no-execution", self.rules(lint, paths=paths))
        # An unrelated subprocess elsewhere in the class does not cover the fixture.
        unrelated = lint + '    def test_tool(self):\n        subprocess.run(["bash", "--version"])\n'
        self.assertIn("workflow-no-execution", self.rules(unrelated, paths=paths))

    def test_temp_config_even_with_same_tracked_filename_stays_quiet(self) -> None:
        source = '''
class Tests:
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.repo = Path(self.directory.name)
    def test_fixture(self):
        config = json.loads((self.repo / "package.json").read_text())
        self.assertEqual(config["name"], "fixture")
'''
        self.assertEqual(self.rules(source, paths={"package.json"}), set())

    def test_source_agreement_and_behavior_messages_stay_quiet(self) -> None:
        source = '''
fixture = 'assert version == "4.5.6"'
assert result.version == metadata.version
self.assertEqual(response.status_code, 409)
self.assertIn("Do not run the marking helpers yourself", hook.reminder(marker))
self.assertEqual(response.message, "The requested operation could not be completed.")
assert artifact.digest == expected_digest
'''
        self.assertEqual(self.rules(source, versions={"4.5.6"}), set())

    def test_parsed_execution_output_and_scanner_output_are_behavior(self) -> None:
        source = '''
r = subprocess.run(["uv", "run", "scripts/tool.py", "--json"])
data = json.loads(r.stdout)
self.assertIn("ok", data["status"])
result = scan('assert version == "4.5.6"')
self.assertIn("duplicated-version", result)
self.assertEqual(settings.host, "127.0.0.1")
'''
        self.assertEqual(self.rules(source, paths={"scripts/tool.py"}, versions={"4.5.6", "127.0.0"}), set())
        self.assertEqual(guard.VERSION.findall("127.0.0.1"), [])

    def test_unpacked_temporary_paths_and_templates(self) -> None:
        fixture = '''
class Tests:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.first, self.second = self.root / "one", self.root / "two"
    def test_fixture(self):
        self.assertIn("private wording", (self.first / "AGENTS.md").read_text())
'''
        self.assertEqual(self.rules(fixture, paths={"AGENTS.md"}, catalog=True), set())
        source = 'template_path = ROOT / "templates/helper.py"\nself.assertIn("function_name", template_path.read_text())'
        self.assertIn("source-text", self.rules(source, paths={"templates/helper.py"}))
        fixture = '''
with tempfile.TemporaryDirectory() as directory:
    manifest, expected = build_fixture(Path(directory))
    workspace = Path(manifest["workspace"])
    self.assertNotIn("private wording", (workspace / "AGENTS.md").read_text())
'''
        self.assertEqual(self.rules(fixture, paths={"AGENTS.md"}, catalog=True), set())

    def test_config_types_and_source_extensions(self) -> None:
        for path in ("pyproject.toml", "uv.lock", "Dockerfile.release", "docker-compose.test.yml",
                     ".github/github.json", "Config/App.entitlements", "Config/Info.plist", "package.json"):
            with self.subTest(path=path):
                source = f'config = load_config(ROOT / "{path}")\nself.assertEqual(config["field"], "literal")'
                self.assertIn("pinned-config", self.rules(source, paths={path}))
        for path in ("scripts/helper.py", "scripts/helper.js", "scripts/helper.sh"):
            source = f'self.assertIn("function_name", (ROOT / "{path}").read_text())'
            self.assertIn("source-text", self.rules(source, paths={path}))

    def test_parse_error_is_visible(self) -> None:
        self.assertIn("parse-error", self.rules("def broken("))

    def test_test_file_selection(self) -> None:
        for path in ("scripts/test_guard.py", "appTests/ReleaseTests.swift", "web/foo.test.tsx", "__tests__/foo.js"):
            self.assertTrue(guard.is_test(path), path)
        for path in ("src/release.py", "tests/README.md", "test-fixtures/example.py"):
            self.assertFalse(guard.is_test(path), path)


class Command(unittest.TestCase):
    def test_tracked_source_agreement_warning_exit_and_exclusion(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True)
            test = repo / "test_candidate.py"
            test.write_text('assert version == "4.5.6"\n')
            (repo / "product.py").write_text('VERSION = "4.5.6"\n')
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            (repo / "test_untracked.py").write_text('assert version == "4.5.6"\n')
            command = [sys.executable, guard.__file__, "--repo", directory]
            result = subprocess.run([*command, "--format", "json"], capture_output=True, text=True, check=True)
            findings = json.loads(result.stdout)["findings"]
            self.assertEqual([(item["path"], item["line"], item["rule"]) for item in findings],
                             [(test.name, 1, "duplicated-version")])
            self.assertEqual(findings[0]["classification"], "hard-failure-candidate")
            excluded = subprocess.run([*command, "--exclude", test.name, "--format", "json"],
                                      capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(excluded.stdout)["findings"], [])
            annotated = subprocess.run([*command, "--format", "github"], capture_output=True, text=True, check=True)
            self.assertIn(f"::warning file={test.name},line=1,", annotated.stdout)
            test.write_text('assert version == metadata.version\n')
            self.assertEqual(guard.scan(repo, []), [])

    def test_invalid_repository_fails_visibly(self) -> None:
        with TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, guard.__file__, "--repo", directory], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stderr)

    def test_gitlinks_and_locally_deleted_files_do_not_crash(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True)
            (repo / "deleted.txt").write_text("fixture")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            (repo / "deleted.txt").unlink()
            (repo / "submodule").mkdir()
            subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                            "160000," + "1" * 40 + ",submodule"], cwd=repo, check=True)
            self.assertEqual(guard.scan(repo, []), [])


if __name__ == "__main__":
    unittest.main()
