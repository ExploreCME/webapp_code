import sqlite3
import unittest
from unittest.mock import MagicMock, mock_open, patch

from report_engine import ReportEngine


class OverallUsageTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE question_bank (
                id INTEGER PRIMARY KEY, organ_system TEXT,
                organ_system_norm TEXT, task_area TEXT
            );
            CREATE TABLE quiz_attempts (
                attempt_id INTEGER PRIMARY KEY, username_norm TEXT, mode TEXT
            );
            CREATE TABLE attempt_responses (
                attempt_id INTEGER, question_id INTEGER, is_correct INTEGER
            );
            CREATE TABLE remediation_questions (id INTEGER, assignment_id INTEGER);
            CREATE TABLE remediation_assignments (
                id INTEGER, organ_system TEXT, task_area TEXT, topic_area TEXT
            );
            INSERT INTO quiz_attempts VALUES (1, 'student', 'test');
        """)
        db_cursor = self.db.cursor()
        cursor = MagicMock()
        cursor.execute.side_effect = lambda sql, params=(): db_cursor.execute(
            sql.replace("%s", "?"), params
        )
        cursor.fetchall.side_effect = lambda: [
            dict(row) for row in db_cursor.fetchall()
        ]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        self.engine = ReportEngine(connection)

    def add_question(self, organ, correct=None, task="formulating most likely diagnosis"):
        question = self.db.execute(
            "INSERT INTO question_bank (organ_system, organ_system_norm, task_area) "
            "VALUES (?, NULL, ?)", (organ, task)
        )
        if correct is not None:
            self.db.execute(
                "INSERT INTO attempt_responses VALUES (1, ?, ?)",
                (question.lastrowid, correct)
            )

    def generate_report(self):
        with patch("builtins.open", mock_open()):
            return self.engine.generate_overall_usage([" Student "])

    def test_null_normalized_organs_use_trimmed_original_labels(self):
        self.add_question(" Cardiovascular System ", correct=1)
        self.add_question("Cardiovascular System", correct=0)
        self.add_question(" Pulmonary System ", correct=1,
                          task="using laboratory and diagnostic studies")

        stats = self.generate_report()

        self.assertEqual(stats["organs"], {
            "Cardiovascular System": {"att": 2, "cor": 1},
            "Pulmonary System": {"att": 1, "cor": 1},
        })
        self.assertNotIn("Miscellaneous", stats["organs"])
        self.assertEqual(stats["tasks"], {
            "Most Likely Dx": {"att": 2, "cor": 1},
            "Labs & Dx": {"att": 1, "cor": 1},
        })
        self.assertEqual(stats["total_attempted"], 3)
        self.assertEqual(stats["total_correct"], 2)
        self.assertEqual(stats["overall_pct"], 66.7)

    def test_organs_outside_top_14_remain_miscellaneous(self):
        for index in range(15):
            organ = f"Organ {index}"
            self.add_question(organ, correct=1)
            for _ in range(14 - index):
                self.add_question(f" {organ} ")
        self.add_question(None, correct=0, task="unmapped task")
        self.add_question("   ", correct=0, task=None)

        stats = self.generate_report()

        self.assertEqual(stats["organs"], {
            **{f"Organ {index}": {"att": 1, "cor": 1} for index in range(14)},
            "Miscellaneous": {"att": 3, "cor": 1},
        })
        self.assertEqual(stats["tasks"], {"Most Likely Dx": {"att": 15, "cor": 15}})


if __name__ == "__main__":
    unittest.main()
