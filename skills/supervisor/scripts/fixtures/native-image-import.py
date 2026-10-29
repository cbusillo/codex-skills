from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


def docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=90)


class NativeImageImportTests(unittest.TestCase):
    def test_archive_import_preserves_immutable_execution_and_refuses_missing_content(
        self,
    ) -> None:
        fixtures = json.loads(Path("/workspace/images.json").read_text())
        for fixture in fixtures:
            with self.subTest(tag=fixture["tag"]):
                for archive in fixture["archives"]:
                    loaded = docker("load", "--input", f"/workspace/{archive}")
                    self.assertEqual(loaded.returncode, 0, loaded.stderr)
                for reference in [fixture["tag"], *fixture["references"]]:
                    with self.subTest(reference=reference):
                        inspected = docker("image", "inspect", reference)
                        self.assertEqual(inspected.returncode, 0, inspected.stderr)
                        image = json.loads(inspected.stdout)[0]
                        if reference in fixture["references"]:
                            self.assertIn(reference, image["RepoDigests"])
                        created = docker("create", "--pull", "never", reference, "true")
                        self.assertEqual(created.returncode, 0, created.stderr)
                        container = created.stdout.strip()
                        try:
                            metadata = docker("inspect", container)
                            self.assertEqual(metadata.returncode, 0, metadata.stderr)
                            self.assertEqual(
                                json.loads(metadata.stdout)[0]["Config"]["Image"],
                                reference,
                            )
                            executed = docker("start", "--attach", container)
                            self.assertEqual(executed.returncode, 0, executed.stderr)
                        finally:
                            removed = docker("rm", "--force", container)
                            self.assertEqual(removed.returncode, 0, removed.stderr)
                for reference in fixture["missing_references"]:
                    with self.subTest(missing_reference=reference):
                        self.assertNotEqual(docker("image", "inspect", reference).returncode, 0)
                        refused = docker("run", "--rm", "--pull", "never", reference, "true")
                        self.assertNotEqual(refused.returncode, 0, refused.stderr)


if __name__ == "__main__":
    unittest.main()
