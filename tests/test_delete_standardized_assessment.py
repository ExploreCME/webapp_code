from contextlib import redirect_stderr
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import delete_standardized_assessment as script


ROW = {
    "quiz_name": "Emergency Medicine version 1",
    "category": "Emergency Medicine",
    "question_ids": json.dumps(["std-1", "std-2", "std-3"]),
}


def make_connection(row=ROW, rowcount=1):
    connection = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    cursor.rowcount = rowcount
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection, cursor


def inputs(*values):
    return MagicMock(side_effect=list(values))


class BuildQuizNameTests(unittest.TestCase):
    def test_formats_published_name(self):
        self.assertEqual(
            script.build_quiz_name(" Emergency Medicine ", " 01 "),
            "Emergency Medicine version 1",
        )

    def test_rejects_invalid_versions(self):
        for version in ("", "0", "-1", "1.5", "abc", "²"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    script.build_quiz_name("Emergency Medicine", version)

    def test_rejects_blank_name(self):
        with self.assertRaises(ValueError):
            script.build_quiz_name("  ", "1")


class DeleteStandardizedAssessmentTests(unittest.TestCase):
    def run_main(self, connection, *answers):
        output = []
        result = script.main(
            input_func=inputs(*answers),
            print_func=lambda *args: output.append(" ".join(map(str, args))),
            connect=lambda: connection,
        )
        return result, "\n".join(output)

    def executed_sql(self, cursor):
        return [" ".join(call.args[0].split()) for call in cursor.execute.call_args_list]

    def test_confirmed_delete_commits_single_row(self):
        connection, cursor = make_connection()
        result, output = self.run_main(
            connection, "Emergency Medicine", "1",
            "DELETE Emergency Medicine version 1",
        )

        self.assertTrue(result)
        connection.begin.assert_called_once()
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()
        connection.close.assert_called_once()
        self.assertIn("Questions: 3", output)
        self.assertIn("Category:  Emergency Medicine", output)
        self.assertIn("Deleted published assessment", output)

        select_sql, delete_sql = self.executed_sql(cursor)
        self.assertIn("WHERE BINARY quiz_name = %s FOR UPDATE", select_sql)
        self.assertEqual(
            delete_sql,
            "DELETE FROM standardized_quiz_versions WHERE BINARY quiz_name = %s",
        )
        for call in cursor.execute.call_args_list:
            self.assertEqual(call.args[1], ("Emergency Medicine version 1",))

    def test_only_standardized_quiz_versions_is_modified(self):
        connection, cursor = make_connection()
        self.run_main(
            connection, "Emergency Medicine", "1",
            "DELETE Emergency Medicine version 1",
        )
        statements = self.executed_sql(cursor)
        deletes = [sql for sql in statements if sql.upper().startswith("DELETE")]
        self.assertEqual(len(deletes), 1)
        for sql in statements:
            self.assertIn("standardized_quiz_versions", sql)
            self.assertNotIn("question_bank", sql)
            self.assertNotIn("attempt", sql.lower())

    def test_not_found_rolls_back(self):
        connection, cursor = make_connection(row=None)
        prompt = inputs("Emergency Medicine", "2")
        output = []
        result = script.main(
            input_func=prompt, print_func=output.append, connect=lambda: connection,
        )

        self.assertFalse(result)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(cursor.execute.call_count, 1)
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        connection.close.assert_called_once()
        self.assertIn("No exact match found", "\n".join(output))

    def test_confirmation_mismatch_cancels(self):
        connection, cursor = make_connection()
        result, output = self.run_main(
            connection, "Emergency Medicine", "1",
            "delete Emergency Medicine version 1",
        )

        self.assertFalse(result)
        self.assertEqual(cursor.execute.call_count, 1)
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        connection.close.assert_called_once()
        self.assertIn("Cancelled. Nothing was deleted.", output)

    def test_invalid_version_never_connects(self):
        connect = MagicMock()
        with self.assertRaises(ValueError):
            script.main(
                input_func=inputs("Emergency Medicine", "zero"),
                print_func=lambda *args: None, connect=connect,
            )
        connect.assert_not_called()

    def test_unexpected_rowcount_rolls_back(self):
        connection, _ = make_connection(rowcount=0)
        with self.assertRaises(RuntimeError):
            self.run_main(
                connection, "Emergency Medicine", "1",
                "DELETE Emergency Medicine version 1",
            )
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        connection.close.assert_called_once()

    def test_database_error_rolls_back_and_closes(self):
        connection, cursor = make_connection()
        cursor.execute.side_effect = RuntimeError("db down")
        with self.assertRaises(RuntimeError):
            self.run_main(connection, "Emergency Medicine", "1")
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_run_returns_nonzero_on_failure(self):
        with patch.object(script, "main", side_effect=ValueError("bad version")):
            with redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(script.run(), 1)
        self.assertIn("Failed: bad version", stderr.getvalue())

    def test_run_returns_zero_on_success(self):
        with patch.object(script, "main", return_value=True):
            self.assertEqual(script.run(), 0)

    def test_run_returns_nonzero_when_nothing_deleted(self):
        with patch.object(script, "main", return_value=False):
            self.assertEqual(script.run(), 1)


if __name__ == "__main__":
    unittest.main()
