#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prepare-python-project.py"
SPEC = importlib.util.spec_from_file_location("prepare_python_project", SCRIPT_PATH)
prepare_python_project = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(prepare_python_project)


class PreparePythonProjectTest(unittest.TestCase):
    def test_sdk_name_uses_home_relative_form(self):
        self.assertEqual(
            prepare_python_project.sdk_name_for(Path("/Users/example/work/.venv"), Path("/Users/example")),
            "~/work/.venv",
        )

    def test_rendered_project_model_uses_sdk_and_test_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            with patch.object(prepare_python_project.Path, "home", return_value=repo.parent):
                files = prepare_python_project.render_project_files(repo, "codex-skills", ["jetbrains-inspection/tests"])

        module = files[repo / ".idea" / "codex-skills.iml"]
        self.assertIn('content url="file://$MODULE_DIR$"', module)
        self.assertIn('isTestSource="true"', module)
        self.assertIn('jdkType="Python SDK"', module)
        self.assertIn("jetbrains-inspection/tests", module)
        self.assertIn("codex-skills.iml", files[repo / ".idea" / "modules.xml"])
        self.assertIn("project-jdk-name=", files[repo / ".idea" / "misc.xml"])

    def sdk_table(self, path, entries):
        root = ElementTree.Element("application")
        component = ElementTree.SubElement(root, "component", name="ProjectJdkTable")
        for name, home, kind in entries:
            sdk = ElementTree.SubElement(component, "jdk")
            for tag, value in [("name", name), ("homePath", home), ("type", kind)]:
                ElementTree.SubElement(sdk, tag, value=value)
        ElementTree.ElementTree(root).write(path, encoding="unicode")

    def test_registered_sdk_identity_uses_interpreter_home_not_display_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            repo = home / "repo"
            table = home / "jdk.table.xml"
            self.sdk_table(table, [
                ("uv (repo) & Python", "$USER_HOME$/repo/.venv/bin/python", "Python SDK"),
                ("wrong", str(repo / ".venv/bin/python"), "JavaSDK"),
                ("other", str(home / "other/.venv/bin/python"), "Python SDK"),
            ])
            with patch.object(prepare_python_project.Path, "home", return_value=home):
                name = prepare_python_project.registered_sdk_name(repo / ".venv", table)
            self.assertEqual(name, "uv (repo) & Python")
            files = prepare_python_project.render_project_files(repo, "repo", [], name)
            self.assertEqual(ElementTree.fromstring(files[repo / ".idea/misc.xml"]).find("component").get("project-jdk-name"), name)
            self.assertEqual(ElementTree.fromstring(files[repo / ".idea/repo.iml"]).find("./component/orderEntry").get("jdkName"), name)

    def test_sdk_lookup_does_not_follow_interpreter_symlinks_across_worktrees(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "python"
            shared.touch()
            for name in ["one", "two"]:
                interpreter = root / name / ".venv/bin/python"
                interpreter.parent.mkdir(parents=True)
                interpreter.symlink_to(shared)
            table = root / "jdk.table.xml"
            self.sdk_table(table, [("other", str(root / "two/.venv/bin/python"), "Python SDK")])
            self.assertIsNone(prepare_python_project.registered_sdk_name(root / "one/.venv", table))

    def test_cli_binds_both_models_to_registered_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / ".gitignore").write_text(".idea/\n.venv/\n", encoding="utf-8")
            (repo / ".venv").mkdir()
            table = repo / "jdk.table.xml"
            self.sdk_table(table, [("uv (fixture)", str(repo / ".venv/bin/python"), "Python SDK")])
            with (
                patch.object(sys, "argv", [str(SCRIPT_PATH), "--repo", str(repo), "--python", "3.12",
                                           "--module-name", "fixture", "--sdk-table", str(table)]),
                patch.object(prepare_python_project, "validate_existing_venv"),
            ):
                self.assertEqual(prepare_python_project.main(), 0)
            misc = ElementTree.parse(repo / ".idea/misc.xml").getroot()
            module = ElementTree.parse(repo / ".idea/fixture.iml").getroot()
            self.assertEqual(misc.find("component").get("project-jdk-name"), "uv (fixture)")
            self.assertEqual(module.find("./component/orderEntry").get("jdkName"), "uv (fixture)")

    def test_sdk_lookup_missing_ambiguous_and_malformed_tables(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            table = root / "jdk.table.xml"
            self.assertIsNone(prepare_python_project.registered_sdk_name(root / ".venv", None))
            self.assertIsNone(prepare_python_project.registered_sdk_name(root / ".venv", table))
            self.sdk_table(table, [(n, str(root / ".venv/bin/python"), "Python SDK") for n in ["one", "two"]])
            with self.assertRaisesRegex(RuntimeError, "Multiple Python SDK names"):
                prepare_python_project.registered_sdk_name(root / ".venv", table)
            table.write_text("<application>", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Cannot read"):
                prepare_python_project.registered_sdk_name(root / ".venv", table)

    def test_atomic_write_skips_unchanged_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "file.xml"
            prepare_python_project.atomic_write(path, "same\n")
            before = path.stat().st_mtime_ns
            prepare_python_project.atomic_write(path, "same\n")
            self.assertEqual(path.stat().st_mtime_ns, before)

    def test_preflight_requires_ignored_untracked_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / ".gitignore").write_text(".venv/\n.idea/misc.xml\n", encoding="utf-8")
            prepare_python_project.ensure_ignored_outputs(
                repo,
                [repo / ".venv", repo / ".idea" / "misc.xml"],
            )

            (repo / ".idea").mkdir()
            (repo / ".idea" / "misc.xml").write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "add", "-f", ".idea/misc.xml"], cwd=repo, check=True)
            with self.assertRaisesRegex(RuntimeError, "must not be tracked"):
                prepare_python_project.ensure_ignored_outputs(repo, [repo / ".idea" / "misc.xml"])

    def test_sync_command_uses_python_and_requested_extras(self):
        self.assertEqual(
            prepare_python_project.build_sync_command("3.13", ["dev", "docs"]),
            [
                "uv",
                "sync",
                "--locked",
                "--python",
                "3.13",
                "--extra",
                "dev",
                "--extra",
                "docs",
            ],
        )


if __name__ == "__main__":
    unittest.main()
