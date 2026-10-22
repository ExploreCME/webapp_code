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
        for format_ in (None, "pdf", "txt"):
            with self.subTest(format=format_):
                self.check_output_destinations(format_)

    def check_output_destinations(self, format_):
        for explicit in (False, True):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "home"
                extension = format_ or "pdf"
                expected = (
                    Path(tmp) / "custom" / "nested" / "answer-key.custom"
                    if explicit else home / "Downloads" / f"Family_Medicine_version_1.{extension}"
                )
                args = ["--quiz-name", "Family Medicine", "--version", "1"]
                if format_:
                    args += ["--format", format_]
                if explicit:
                    args += ["--output", str(expected)]
                conn = MagicMock()
                with patch.object(Path, "home", return_value=home), \
                        patch.object(self.exporter, "get_db_connection", return_value=conn), \
                        patch.object(self.exporter, "load_version", return_value=(self.meta, [])), \
                        patch("builtins.print") as output:
                    self.assertEqual(self.exporter.main(args), 0)
                if extension == "pdf":
                    self.assertTrue(expected.read_bytes().startswith(b"%PDF-"))
                else:
                    self.assertIn(self.meta["quiz_name"], expected.read_text(encoding="utf-8"))
                output.assert_called_once_with(f"Wrote 0 questions to {expected.resolve()}")
                conn.close.assert_called_once()
                if explicit:
                    self.assertFalse((home / "Downloads").exists())

    def test_text_content(self):
        question = {
            "question_stem": "Café <test> & symptoms?\nSecond line",
            "organ_system": "Cardiovascular",
            "task_area": "Diagnosis",
            "topic_area": "Hypertension",
            **{f"answer_choice_{choice}": f"Option {choice}" for choice in "ABCDE"},
            "correct_answer": " b ",
            "explanation": "Because α > β.\nMore detail.",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "answer-key.txt"
            self.exporter.build_txt(path, self.meta, [question, {}])
            text = path.read_text(encoding="utf-8")
        for expected in (
            self.meta["quiz_name"], "Category: Family Medicine", "Questions: 2",
            "Published: 2026-01-01", "CONFIDENTIAL - answer key",
            "Question 1", "Question 2", question["question_stem"],
            "Cardiovascular / Diagnosis / Hypertension",
            "Correct answer: B. Option B", f"Explanation: {question['explanation']}",
        ):
            self.assertIn(expected, text)
        for choice in "ABCDE":
            self.assertIn(f"{choice}. Option {choice}\n", text)
        self.assertEqual(text.count("=" * 70), 2)
        self.assertNotIn("None", text)

    def test_list_does_not_require_output_or_prompts(self):
        for format_ in ("pdf", "txt"):
            with self.subTest(format=format_):
                conn = MagicMock()
                with patch.object(self.exporter, "get_db_connection", return_value=conn), \
                        patch.object(self.exporter, "list_versions") as list_versions, \
                        patch.object(self.exporter, "load_version") as load_version, \
                        patch.object(self.exporter, "build_pdf") as pdf, \
                        patch.object(self.exporter, "build_txt") as txt, \
                        patch("builtins.input") as prompt:
                    self.assertEqual(self.exporter.main(["--list", "--format", format_]), 0)
                list_versions.assert_called_once()
                for unused in (load_version, pdf, txt, prompt):
                    unused.assert_not_called()
                conn.close.assert_called_once()

    def test_text_write_error_returns_one_and_closes_connection(self):
        conn = MagicMock()
        with patch.object(self.exporter, "get_db_connection", return_value=conn), \
                patch.object(self.exporter, "load_version", return_value=(self.meta, [])), \
                patch.object(Path, "mkdir"), \
                patch.object(self.exporter, "build_txt", side_effect=OSError("write failed")), \
                patch("sys.stderr") as stderr:
            result = self.exporter.main([
                "--quiz-name", "Family Medicine", "--version", "1", "--format", "txt",
            ])
        self.assertEqual(result, 1)
        self.assertIn("Error: write failed", "".join(call.args[0] for call in stderr.write.call_args_list))
        conn.close.assert_called_once()

    def test_invalid_format_is_rejected_before_database_access(self):
        with patch.object(self.exporter, "get_db_connection") as connect, \
                patch("sys.stderr"), self.assertRaises(SystemExit) as error:
            self.exporter.main(["--format", "csv"])
        self.assertEqual(error.exception.code, 2)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
