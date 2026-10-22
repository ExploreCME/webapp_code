import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]


class ExportStandardizedQuizPdfTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "export_standardized_quiz_pdf", ROOT / "export_standardized_quiz_pdf.py"
        )
        self.exporter = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "db_pool": MagicMock(),
            "standardized_quizzes": MagicMock(STANDARDIZED_PREFIX="Standardized: "),
        }):
            spec.loader.exec_module(self.exporter)
        self.meta = {
            "quiz_name": "Family Medicine version 1",
            "category": "Family Medicine",
            "created_date": "2026-01-01",
        }

    def test_output_destinations(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "home"
                expected = (
                    Path(tmp) / "custom" / "nested" / "answer-key.pdf"
                    if explicit else home / "Downloads" / "Family_Medicine_version_1.pdf"
                )
                args = ["--quiz-name", "Family Medicine", "--version", "1"]
                if explicit:
                    args += ["--output", str(expected)]
                conn = MagicMock()
                with patch.object(Path, "home", return_value=home), \
                        patch.object(self.exporter, "get_db_connection", return_value=conn), \
                        patch.object(self.exporter, "load_version", return_value=(self.meta, [])), \
                        patch("builtins.print") as output:
                    self.assertEqual(self.exporter.main(args), 0)
                self.assertTrue(expected.read_bytes().startswith(b"%PDF-"))
                output.assert_called_once_with(f"Wrote 0 questions to {expected.resolve()}")
                conn.close.assert_called_once()
                if explicit:
                    self.assertFalse((home / "Downloads").exists())


if __name__ == "__main__":
    unittest.main()
