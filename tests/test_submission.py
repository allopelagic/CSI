import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.build_submission import build


class SubmissionTests(unittest.TestCase):
    def test_code_only_archive_excludes_caches_and_has_expected_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            output = build(Path(directory) / "code.zip", code_only=True)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                self.assertTrue({"Codes/server.py", "Codes/client.py", "Codes/questions.json",
                                 "Codes/README.md", "Codes/tests/test_integration.py"} <= names)
                self.assertTrue(all(name.startswith("Codes/") for name in names))
                self.assertFalse(any("__pycache__" in name or ".git/" in name or ".venv/" in name for name in names))
                self.assertIsNone(archive.testzip())
            with self.assertRaises(FileExistsError):
                build(output, code_only=True)

    def test_missing_or_invalid_report_not_silently_submitted(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "submission.zip"
            with self.assertRaises(ValueError):
                build(output)
            path = Path(directory) / "report.pdf"
            path.write_text("not a PDF")
            with self.assertRaises(ValueError):
                build(output, path)
            self.assertFalse(output.exists())
