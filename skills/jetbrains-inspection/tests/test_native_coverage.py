#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Physical execution and independent defect-canary coverage contracts."""
import importlib.util
import json
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts/native_coverage.py"
spec = importlib.util.spec_from_file_location("native_coverage_tests", SCRIPT)
assert spec is not None and spec.loader is not None
coverage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage)

CONTROL_ROOT = "jb_native_controls_current"


def canary(tool, *, root=CONTROL_ROOT, file=None):
    return {"rule_id": tool, "file": root + "/" + (file or coverage.CANARY_FILES[tool])}


class CoverageTests(unittest.TestCase):
    @staticmethod
    def assess(requested, traces, *, selected=None, registered=None, findings=None, failed=(), languages=None, scope="files"):
        return coverage.assess(
            set(requested), set(requested) if registered is None else set(registered), traces, set(failed),
            {"models.py"} if selected is None else set(selected),
            [canary(tool) for tool in requested if tool in coverage.CANARY_FILES] if findings is None else findings,
            control_root=CONTROL_ROOT, tool_languages=languages, scope=scope,
        )

    def test_yaml_and_general_checks_require_execution_on_each_selected_file(self) -> None:
        tools = {"InconsistentLineSeparators", "IncorrectFormatting", "ProblematicWhitespace", "TodoComment", "YAMLDuplicatedKeys"}
        selected = {"compose.yml", "workflow.yaml"}
        traces = {tool: selected for tool in tools}
        result, gaps = self.assess(tools, traces, selected=selected)
        self.assertTrue(result["complete"], gaps)
        for tool in tools:
            with self.subTest(tool=tool):
                incomplete = {**traces, tool: {"compose.yml"}}
                result, _ = self.assess(tools, incomplete, selected=selected)
                self.assertFalse(result["complete"])
                self.assertEqual(result["missing_execution"][tool], ["workflow.yaml"])

    def test_yaml_language_metadata_does_not_drop_checks_from_mixed_selections(self) -> None:
        selected = {"model.py", "compose.yml", "workflow.yaml"}
        result, gaps = self.assess({"VendorYamlRule", "PyCompatibilityInspection"},
                                  {"VendorYamlRule": selected - {"model.py"}, "PyCompatibilityInspection": {"model.py"}},
                                  selected=selected, languages={"VendorYamlRule": "YAML"})
        self.assertTrue(result["complete"], gaps)
        self.assertEqual(result["selected_execution"]["VendorYamlRule"], sorted(selected - {"model.py"}))
        result, _ = self.assess({"VendorYamlRule"}, {}, selected=selected, languages={"VendorYamlRule": "YAML"})
        self.assertFalse(result["complete"])

    def test_directory_check_is_inapplicable_only_to_file_scopes(self) -> None:
        for scope in ("files", "changed_files", "directory", "whole_project"):
            with self.subTest(scope=scope):
                tools = {"EmptyDirectory", "TodoComment"}
                result, _ = self.assess(tools, {"TodoComment": {"compose.yml"}}, selected={"compose.yml"}, scope=scope)
                self.assertEqual(result["complete"], scope in {"files", "changed_files"})

    def test_controls_and_user_findings_cannot_replace_selected_execution(self):
        tool = "PyTypeCheckerInspection"
        result, gaps = self.assess({tool}, {tool: {canary(tool)["file"]}},
                                   findings=[canary(tool), {"rule_id": tool, "file": "models.py"}])
        self.assertFalse(result["complete"])
        self.assertTrue(result["canary_results"][tool]["detected"])
        self.assertEqual(result["selected_execution"][tool], [])
        self.assertTrue(gaps)

    def test_each_selected_file_requires_the_tool(self):
        tool = "PyCompatibilityInspection"
        result, _ = self.assess({tool}, {tool: {"one.py"}}, selected={"one.py", "two.py"})
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing_execution"][tool], ["two.py"])

    def test_clean_selected_files_have_complete_proof_without_user_findings(self):
        tool = "PyTypeCheckerInspection"
        result, gaps = self.assess({tool}, {tool: {"models.py"}})
        self.assertTrue(result["complete"])
        self.assertEqual(result["selected_execution"][tool], ["models.py"])
        self.assertFalse(gaps)

    def test_canaries_need_the_exact_rule_and_current_source_file(self):
        tool = "PyTypeCheckerInspection"
        invalid = [canary(tool, root="jb_native_controls_previous"),
                   canary(tool, file="coverage.py"),
                   {"rule_id": "PyUnresolvedReferencesInspection", "file": canary(tool)["file"]}]
        for finding in invalid:
            with self.subTest(finding=finding):
                result, _ = self.assess({tool}, {tool: {"models.py"}}, findings=[finding])
                self.assertFalse(result["complete"])
                self.assertFalse(result["canary_results"][tool]["detected"])

    def test_python_and_xml_applicability_are_independent(self):
        tools = {"OdooPy", "OdooXml"}
        python, _ = self.assess(tools, {"OdooPy": {"models.py"}})
        self.assertTrue(python["complete"])
        self.assertIn("OdooXml", python["not_applicable"])
        self.assertNotIn("OdooXml", python["canary_results"])
        xml, _ = self.assess(tools, {"OdooXml": {"view.xml"}}, selected={"view.xml"})
        self.assertTrue(xml["complete"])
        self.assertIn("OdooPy", xml["not_applicable"])

    def test_mixed_scope_requires_xml_tool_on_user_xml_not_control(self):
        tool = "OdooXml"
        result, _ = self.assess({tool}, {tool: {canary(tool)["file"]}}, selected={"models.py", "view.xml"})
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing_execution"][tool], ["view.xml"])

    def test_failed_selected_pair_cannot_borrow_another_success(self):
        tool = "PyTypeCheckerInspection"
        result, gaps = self.assess({tool}, {tool: {"models.py"}}, failed={(tool, "models.py")})
        self.assertFalse(result["complete"])
        self.assertEqual(result["selected_execution"][tool], [])
        self.assertTrue(any(gap.startswith("inspection_execution_failed:") for gap in gaps))

    def test_failed_control_does_not_pass_canary_even_when_a_finding_exists(self):
        tool = "PyTypeCheckerInspection"
        result, _ = self.assess({tool}, {tool: {"models.py"}}, failed={(tool, canary(tool)["file"])})
        self.assertFalse(result["complete"])
        self.assertFalse(result["canary_results"][tool]["detected"])

    def test_unknown_applicability_is_not_proved_by_arbitrary_spans(self):
        tool = "UnfamiliarVendorTool"
        result, _ = self.assess({tool}, {tool: {"models.py"}})
        self.assertFalse(result["complete"])
        self.assertIn(tool, result["applicability_unproven"])

    def test_registered_language_can_establish_custom_tool_applicability(self):
        tool = "VendorModelRule"
        result, _ = self.assess({tool}, {tool: {"models.py"}}, languages={tool: "Python"})
        self.assertTrue(result["complete"])

    def test_vendor_language_identities_are_case_insensitive(self):
        tool = "VendorModelRule"
        for language in ("python", "PYTHON", "Python"):
            with self.subTest(language=language):
                result, _ = self.assess({tool}, {tool: {"models.py"}}, languages={tool: language})
                self.assertTrue(result["complete"])
        for language in ("xml", "XML", "Xml"):
            with self.subTest(language=language):
                result, _ = self.assess({tool}, {tool: {"view.xml"}}, selected={"view.xml"}, languages={tool: language})
                self.assertTrue(result["complete"])

    def test_known_other_language_identities_are_case_insensitive(self):
        for language in ("yaml", "YAML", "javascript", "JAVASCRIPT", "kotlin", "KOTLIN"):
            with self.subTest(language=language):
                result, _ = self.assess({"VendorOtherRule", "PyCompatibilityInspection"},
                                        {"PyCompatibilityInspection": {"models.py"}},
                                        languages={"VendorOtherRule": language})
                self.assertTrue(result["complete"])
                self.assertIn("VendorOtherRule", result["not_applicable"])
        result, _ = self.assess({"VendorOtherRule"}, {"VendorOtherRule": {"models.py"}},
                                languages={"VendorOtherRule": "unrecognized-language"})
        self.assertFalse(result["complete"])
        self.assertIn("VendorOtherRule", result["applicability_unproven"])

    def test_other_language_profile_entries_are_explicitly_not_applicable(self):
        tools = {"PyCompatibilityInspection", "CssUnknownProperty", "Eslint"}
        result, _ = self.assess(tools, {"PyCompatibilityInspection": {"models.py"}},
                                registered={"PyCompatibilityInspection"})
        self.assertTrue(result["complete"])
        self.assertEqual(set(result["not_applicable"]), tools - {"PyCompatibilityInspection"})

    def test_other_language_tools_are_never_omitted_for_files_of_their_language(self):
        cases = (("VendorJavaRule", "JAVA", "src/Main.java"), ("VendorKotlinRule", "kotlin", "src/Main.kt"),
                 ("VendorTypeScriptRule", "TypeScript", "web/app.ts"), ("VendorScriptRule", "JavaScript", "web/view.tsx"),
                 ("CssUnknownProperty", None, "web/site.scss"), ("Eslint", None, "web/app.ts"),
                 ("Eslint", None, "web/App.svelte"), ("VendorJavaRule", "JAVA", "build.gradle"),
                 ("VendorSqlRule", "SQL", "db/schema.pgsql"))
        for tool, language, path in cases:
            with self.subTest(tool=tool, path=path):
                result, _ = self.assess({tool, "PyCompatibilityInspection"}, {"PyCompatibilityInspection": {"models.py"}},
                                        selected={"models.py", path}, languages={tool: language} if language else None)
                self.assertNotIn(tool, result["not_applicable"])
                self.assertFalse(result["complete"])

    def test_vendor_migration_requires_every_successor_on_selected_files(self):
        old = "PyUnusedLocalInspection"
        children = coverage.UNUSED_SPLIT
        traces = {tool: {"models.py"} for tool in children}
        result, _ = self.assess({old}, traces, registered=children)
        self.assertTrue(result["complete"])
        self.assertEqual(set(result["vendor_migrations"][old]), children)
        missing = next(iter(children))
        del traces[missing]
        result, _ = self.assess({old}, traces, registered=children)
        self.assertFalse(result["complete"])
        self.assertIn(missing, result["unproven"])

    def test_incomplete_vendor_migration_stays_unavailable(self):
        children = coverage.UNUSED_SPLIT
        registered = children - {next(iter(children))}
        result, _ = self.assess({"PyUnusedLocalInspection"}, {}, registered=registered)
        self.assertFalse(result["complete"])
        self.assertIn("PyUnusedLocalInspection", result["unavailable"])

    def test_empty_selection_does_not_vacuously_pass(self):
        result, gaps = self.assess({"PyCompatibilityInspection"}, {}, selected=set())
        self.assertFalse(result["complete"])
        self.assertIn("native_scope_empty", gaps)


class TraceTests(unittest.TestCase):
    def test_stock_fixture_retains_actual_physical_pairs(self):
        fixture = Path(__file__).parent / "fixtures/native-trace/clean-tools.json"
        proved, failed = coverage.read_trace(fixture, Path("/fixture/project"))
        self.assertIn("src/example.py", proved["PyCompatibilityInspection"])
        self.assertFalse(failed)

    def test_failed_stock_span_never_borrows_a_successful_duplicate(self):
        fixture = Path(__file__).parent / "fixtures/native-trace/clean-tools.json"
        payload = json.loads(fixture.read_text())
        original = payload["data"][0]["spans"][0]
        for key, value in (("error", "true"), ("otel.status_code", "ERROR")):
            with self.subTest(key=key):
                broken = json.loads(json.dumps(original))
                broken["tags"].append({"key": key, "value": value})
                result, failed = coverage.trace_evidence({"data": [{"spans": [original, broken]}]}, Path("/fixture/project"))
                tool = next(tag["value"] for tag in original["tags"] if tag["key"] == "inspectionId")
                self.assertFalse(result[tool])
                self.assertIn((tool, "src/example.py"), failed)

    def test_synthetic_and_outside_paths_never_prove_a_physical_file(self):
        fixture = Path(__file__).parent / "fixtures/native-trace/clean-tools.json"
        original = json.loads(fixture.read_text())["data"][0]["spans"][0]
        for outside in ("/example.py", "/other/project/src/example.py", "/fixture/project/../other.py"):
            with self.subTest(outside=outside):
                clone = json.loads(json.dumps(original))
                next(tag for tag in clone["tags"] if tag["key"] == "file")["value"] = outside
                proved, _ = coverage.trace_evidence({"data": [{"spans": [clone]}]}, Path("/fixture/project"))
                self.assertFalse(proved)

    def test_malformed_trace_does_not_become_empty_success(self):
        for invalid in ({}, {"data": [{}]}, {"data": [{"spans": [None]}]}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    coverage.trace_evidence(invalid, Path("/fixture/project"))

    def test_malformed_tag_containers_uniformly_raise_value_error(self):
        for tags in (None, 1, True, "inspectionId", {}, [None], [{"key": []}]):
            with self.subTest(tags=tags):
                invalid = {"data": [{"spans": [{"operationName": "inspectionRun", "tags": tags}]}]}
                with self.assertRaises(ValueError):
                    coverage.trace_evidence(invalid, Path("/fixture/project"))


if __name__ == "__main__":
    unittest.main()
