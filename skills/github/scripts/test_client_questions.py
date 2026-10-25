# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from check_client_placeholders import check_client_placeholders
from github_plan_status.client_test_notes import (
    CLIENT_PLACEHOLDER_PREFIX,
    ENGINEERING_RELEASE_TESTS,
    OPEN_CLIENT_QUESTIONS,
    body_client_test_notes,
    client_test_notes,
    owner_test_notes,
)


class ClientQuestionsTests(unittest.TestCase):
    def test_question_heading_level_and_decoration_cannot_hide_questions(self) -> None:
        for heading in (OPEN_CLIENT_QUESTIONS, f"**{OPEN_CLIENT_QUESTIONS}**:"):
            for prefix in ("", "#", "##", "###", "####"):
                if not prefix:
                    continue
                body = f"## Client test notes\nTry checkout.\n{prefix} {heading}\n- What is the address?\n## Validation\nPassed."
                with self.subTest(body=body):
                    self.assertTrue(body_client_test_notes(body).error)
                    self.assertTrue(client_test_notes(owner_test_notes(body)).error)

    def test_questions_block_every_notes_class_and_batch_member(self) -> None:
        question = f"\n### {OPEN_CLIENT_QUESTIONS}\n- What is the business address?"
        for steps in (
            "Client change: update the contact page.\nCheck the contact page.",
            "Nothing for the Client to test: CI only.",
            f"{ENGINEERING_RELEASE_TESTS}\nCheck the contact page.",
        ):
            for body in (
                f"## Client test notes\n{steps}{question}",
                f"## Owner test notes\n{steps}{question}\n## Client test notes\nTry checkout.",
                f"## Client test notes\n### #1 Contact\n{steps}{question.replace('###', '####')}\n### #2 Checkout\nTry checkout.",
            ):
                with self.subTest(body=body):
                    intent = body_client_test_notes(body)
                    self.assertTrue(intent.review_required)
                    self.assertTrue(intent.error)

    def test_empty_questions_reenable_notes_but_cannot_replace_missing_steps(
        self,
    ) -> None:
        empty = f"### {OPEN_CLIENT_QUESTIONS}\n<!-- awaiting new questions -->"
        self.assertFalse(client_test_notes(f"Check the page.\n{empty}").error)
        self.assertTrue(client_test_notes(empty).error)
        self.assertTrue(client_test_notes("").error)
        self.assertFalse(client_test_notes(f"{empty}\n### Steps\nCheck the page.").error)
        self.assertFalse(client_test_notes(f"Check this example:\n```md\n{empty}\n```\n").error)

    def test_committed_shipping_markers_fail_and_other_content_is_ignored(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True)
            (root / "app").mkdir()
            (root / "docs").mkdir()
            (root / "app/page.tsx").write_text("Contact us.")
            (root / "docs/example.md").write_text(CLIENT_PLACEHOLDER_PREFIX + " example]]")
            self.commit(root)
            self.assertEqual(check_client_placeholders(root, ["app"]), 0)
            (root / "app/page.tsx").write_text(CLIENT_PLACEHOLDER_PREFIX + " business address]]")
            self.assertEqual(check_client_placeholders(root, ["app"]), 0)
            self.commit(root)
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(check_client_placeholders(root, ["app"]), 1)
            self.assertIn("app/page.tsx", output.getvalue())
            (root / "app/page.tsx").write_text("Contact us.")
            self.commit(root)
            self.assertEqual(check_client_placeholders(root, ["app"]), 0)
            with self.assertRaises(RuntimeError):
                check_client_placeholders(root, ["app"], "unavailable-revision")
            with self.assertRaises(RuntimeError):
                check_client_placeholders(root, ["misspelled-shipping-root"])

    @staticmethod
    def commit(root: Path) -> None:
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-qm",
                "Fixture",
            ],
            cwd=root,
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
