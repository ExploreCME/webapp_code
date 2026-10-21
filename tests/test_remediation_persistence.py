import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]


class RemediationPersistenceTests(unittest.TestCase):
    def setUp(self):
        dependencies = {name: MagicMock() for name in (
            "openai", "dotenv", "db_pool",
            "Question_writing_and_verification_optomized",
        )}
        dependencies["pydantic"] = MagicMock(BaseModel=object)
        with patch.dict(sys.modules, dependencies):
            spec = importlib.util.spec_from_file_location(
                "create_remediation_assignment", ROOT / "create_remediation_assignment.py")
            self.remediation = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.remediation)

        self.conn = MagicMock()
        self.cursor = AsyncMock()
        self.conn.cursor.return_value.__aenter__.return_value = self.cursor
        self.conn.commit = AsyncMock()
        self.conn.rollback = AsyncMock()
        self.remediation.get_async_db_connection = AsyncMock(return_value=self.conn)
        self.data = [{
            "summary": "📋 Summary",
            "questions": [{"question_stem": "🩺 Question", "explanation": "Explanation"}],
        }]

    def save(self):
        return asyncio.run(self.remediation.save_to_database(
            "owner", self.data, "remediation"))

    def test_insert_failures_propagate_and_roll_back(self):
        for failed_insert in (1, 2):
            with self.subTest(failed_insert=failed_insert):
                self.conn.reset_mock()
                error = RuntimeError("insert failed")
                self.cursor.executemany.side_effect = (
                    [error] if failed_insert == 1 else [None, error])

                with self.assertRaises(RuntimeError) as raised:
                    self.save()

                self.assertIs(raised.exception, error)
                self.conn.rollback.assert_awaited_once_with()
                self.conn.commit.assert_not_awaited()
                self.conn.close.assert_called_once_with()
                self.assertEqual(self.cursor.executemany.await_count, failed_insert)

    def test_commit_failure_propagates_and_rolls_back(self):
        error = RuntimeError("commit failed")
        self.conn.commit.side_effect = error

        with self.assertRaises(RuntimeError) as raised:
            self.save()

        self.assertIs(raised.exception, error)
        self.conn.rollback.assert_awaited_once_with()
        self.conn.close.assert_called_once_with()

    def test_success_commits_and_returns_saved_and_existing_ids(self):
        self.data.append({"id": "existing-id"})

        ids = self.save()

        assignments = self.cursor.executemany.call_args_list[0].args[1]
        questions = self.cursor.executemany.call_args_list[1].args[1]
        self.assertEqual(ids, [assignments[0][0], "existing-id"])
        self.assertEqual(questions[0][1], ids[0])
        self.assertEqual(assignments[0][11], "📋 Summary")
        self.assertEqual(questions[0][2], "🩺 Question")
        self.conn.commit.assert_awaited_once_with()
        self.conn.rollback.assert_not_awaited()
        self.conn.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
