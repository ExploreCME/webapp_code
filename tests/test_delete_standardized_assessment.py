from contextlib import redirect_stderr
import io
import json
import sqlite3
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
    cursor.fetchall.side_effect = lambda: {
        "quiz_attempts": [{"attempt_id": "attempt-1"}, {"attempt_id": "attempt-2"}],
        "attempt_responses": [{"id": 1}],
        "question_bank": [{"id": question_id} for question_id in ("std-1", "std-2", "std-3")],
        "quiz_questions_map": [{"map_id": 1}],
        "assigned_quizzes": [{"id": 1}],
        "standardized_quiz_versions": [],
    }[cursor.execute.call_args.args[0].split("FROM ")[1].split()[0]]
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

    def test_confirmed_delete_commits_all_data_once(self):
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
        self.assertIn("Attempts:  2", output)
        self.assertIn("attempt_responses: 1", output)
        self.assertIn("assigned_quizzes: 1", output)
        self.assertIn("quiz_questions_map: 1", output)
        self.assertIn("Back up the database first", output)
        self.assertIn("Deleted published assessment", output)

        select_sql = self.executed_sql(cursor)[0]
        self.assertIn("WHERE BINARY quiz_name = %s FOR UPDATE", select_sql)
        self.assertEqual(
            self.executed_sql(cursor)[-1],
            "DELETE FROM standardized_quiz_versions WHERE BINARY quiz_name = %s",
        )

    def test_deletion_order_and_exact_scope(self):
        connection, cursor = make_connection()
        self.run_main(
            connection, "Emergency Medicine", "1",
            "DELETE Emergency Medicine version 1",
        )
        deletes = [
            call.args for call in cursor.execute.call_args_list
            if call.args[0].lstrip().startswith("DELETE")
        ]
        self.assertEqual(
            [sql.split()[2] for sql, _ in deletes],
            ["attempt_responses", "quiz_attempts", "assigned_quizzes",
             "quiz_questions_map", "question_bank", "standardized_quiz_versions"],
        )
        self.assertIn("SELECT attempt_id FROM quiz_attempts", deletes[0][0])
        for sql, params in deletes[:3]:
            self.assertIn("BINARY quiz_name = %s", sql)
            self.assertEqual(params, ("Standardized: Emergency Medicine version 1",))
        for sql, params in deletes[3:5]:
            self.assertIn("IN (%s, %s, %s)", sql)
            self.assertEqual(params, ("std-1", "std-2", "std-3"))
        self.assertEqual(deletes[-1][1], ("Emergency Medicine version 1",))

    def test_not_found_rolls_back(self):
        connection, cursor = make_connection(row=None)
        prompt = inputs("Emergency Medicine", "2")
        output = []
        result = script.main(
            input_func=prompt, print_func=output.append, connect=lambda: connection,
        )

        self.assertFalse(result)
        self.assertEqual(prompt.call_count, 2)
        self.assertFalse(any(sql.startswith("DELETE") for sql in self.executed_sql(cursor)))
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
        self.assertFalse(any(sql.startswith("DELETE") for sql in self.executed_sql(cursor)))
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
        connection.commit.assert_not_called()
        connection.close.assert_called_once()

    def test_failure_at_each_deletion_rolls_back(self):
        for table in ("attempt_responses", "quiz_attempts", "assigned_quizzes",
                      "quiz_questions_map", "question_bank", "standardized_quiz_versions"):
            for error in (RuntimeError("delete failed"), KeyboardInterrupt()):
                with self.subTest(table=table, error=type(error).__name__):
                    connection, cursor = make_connection()

                    def execute(sql, params):
                        if " ".join(sql.split()).startswith(f"DELETE FROM {table} "):
                            raise error

                    cursor.execute.side_effect = execute
                    with self.assertRaises(type(error)):
                        self.run_main(
                            connection, "Emergency Medicine", "1",
                            "DELETE Emergency Medicine version 1",
                        )
                    connection.rollback.assert_called_once()
                    connection.commit.assert_not_called()
                    connection.close.assert_called_once()

    def test_interrupted_confirmation_rolls_back_without_deletes(self):
        for error in (KeyboardInterrupt(), EOFError()):
            with self.subTest(error=type(error).__name__):
                connection, cursor = make_connection()
                with self.assertRaises(type(error)):
                    script.delete_version(
                        connection, ROW["quiz_name"],
                        input_func=MagicMock(side_effect=error), print_func=lambda *args: None,
                    )
                connection.rollback.assert_called_once()
                connection.commit.assert_not_called()
                self.assertFalse(any(sql.startswith("DELETE") for sql in self.executed_sql(cursor)))

    def test_shared_questions_abort_without_deletes(self):
        connection, cursor = make_connection()
        cursor.fetchall.side_effect = None
        cursor.fetchall.return_value = [
            {"quiz_name": "Other version 1", "question_ids": '["std-1"]'},
        ]
        with self.assertRaisesRegex(ValueError, "shared"):
            self.run_main(connection, "Emergency Medicine", "1")
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        self.assertFalse(any(sql.startswith("DELETE") for sql in self.executed_sql(cursor)))

    def test_invalid_membership_aborts_without_deletes(self):
        for membership in ('{"std-1": true}', '["ordinary-question"]', 'null', '[1]', 'broken'):
            with self.subTest(membership=membership):
                connection, cursor = make_connection(row={**ROW, "question_ids": membership})
                with self.assertRaises(ValueError):
                    self.run_main(connection, "Emergency Medicine", "1")
                connection.rollback.assert_called_once()
                connection.commit.assert_not_called()
                self.assertFalse(any(sql.startswith("DELETE") for sql in self.executed_sql(cursor)))

    def test_empty_membership_skips_question_deletes(self):
        connection, cursor = make_connection(row={**ROW, "question_ids": "[]"})
        result, output = self.run_main(
            connection, "Emergency Medicine", "1", "DELETE Emergency Medicine version 1",
        )
        self.assertTrue(result)
        self.assertIn("Questions: 0", output)
        self.assertFalse(any("IN ()" in sql for sql in self.executed_sql(cursor)))
        self.assertFalse(any(
            sql.startswith("DELETE FROM question_bank")
            or sql.startswith("DELETE FROM quiz_questions_map")
            for sql in self.executed_sql(cursor)
        ))

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

    def test_run_returns_nonzero_on_interrupt(self):
        for error in (KeyboardInterrupt(), EOFError()):
            with patch.object(script, "main", side_effect=error):
                with patch("builtins.print"):
                    self.assertEqual(script.run(), 1)


class HardDeleteDatabaseTests(unittest.TestCase):
    """Exercise the SQL against SQLite with MySQL locking syntax translated."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE standardized_quiz_versions (
                quiz_name TEXT PRIMARY KEY COLLATE NOCASE, category TEXT, question_ids TEXT
            );
            CREATE TABLE question_bank (id TEXT PRIMARY KEY);
            CREATE TABLE quiz_attempts (
                attempt_id TEXT PRIMARY KEY, quiz_name TEXT COLLATE NOCASE
            );
            CREATE TABLE attempt_responses (
                id INTEGER PRIMARY KEY,
                attempt_id TEXT REFERENCES quiz_attempts(attempt_id),
                question_id TEXT REFERENCES question_bank(id)
            );
            CREATE TABLE quiz_questions_map (
                map_id INTEGER PRIMARY KEY, question_id TEXT REFERENCES question_bank(id)
            );
            CREATE TABLE assigned_quizzes (id INTEGER PRIMARY KEY, quiz_name TEXT COLLATE NOCASE);
        """)
        self.name = ROW["quiz_name"]
        self.db.executemany(
            "INSERT INTO standardized_quiz_versions VALUES (?, ?, ?)",
            [(self.name, ROW["category"], ROW["question_ids"]),
             ("Emergency Medicine version 2", ROW["category"], '["std-other"]')],
        )
        self.db.executemany(
            "INSERT INTO question_bank VALUES (?)",
            [(qid,) for qid in ("std-1", "std-2", "std-3", "std-other", "STD-1")],
        )
        names = [script.STANDARDIZED_PREFIX + self.name,
                 "Standardized: Emergency Medicine version 2",
                 self.name, "standardized: Emergency Medicine version 1"]
        for index, name in enumerate(names):
            self.db.execute("INSERT INTO quiz_attempts VALUES (?, ?)", (str(index), name))
            self.db.execute(
                "INSERT INTO attempt_responses VALUES (?, ?, ?)",
                (index, str(index), "std-1" if index == 0 else "std-other"),
            )
            self.db.execute("INSERT INTO assigned_quizzes VALUES (?, ?)", (index, name))
        self.db.executemany(
            "INSERT INTO quiz_questions_map VALUES (?, ?)",
            [(0, "std-1"), (1, "std-other"), (2, "STD-1")],
        )
        self.db.commit()
        self.connection = MagicMock()
        self.connection.begin.side_effect = lambda: self.db.execute("BEGIN")
        self.connection.commit.side_effect = self.db.commit
        self.connection.rollback.side_effect = self.db.rollback
        self.cursor = MagicMock()
        self.connection.cursor.return_value.__enter__.return_value = self.cursor
        self.cursor.execute.side_effect = self.execute

    def execute(self, sql, params):
        sql = sql.replace(" FOR UPDATE", "").replace("%s", "?")
        for column in ("quiz_name", "question_id", "id"):
            sql = sql.replace(f"BINARY {column}", f"{column} COLLATE BINARY")
        result = self.db.execute(sql, params)
        self.cursor.rowcount = result.rowcount
        self.cursor.fetchone.side_effect = result.fetchone
        self.cursor.fetchall.side_effect = result.fetchall

    def snapshot(self):
        return {
            table: [tuple(row) for row in self.db.execute(f"SELECT * FROM {table}")]
            for table in ("standardized_quiz_versions", "question_bank", "quiz_attempts",
                          "attempt_responses", "assigned_quizzes", "quiz_questions_map")
        }

    def delete(self, confirmation=None):
        return script.delete_version(
            self.connection, self.name,
            input_func=inputs(confirmation if confirmation is not None else f"DELETE {self.name}"),
            print_func=lambda *args: None,
        )

    def test_hard_delete_preserves_other_versions_case_variants_and_unprefixed_quizzes(self):
        self.assertTrue(self.delete())
        remaining = self.snapshot()
        self.assertEqual(remaining["question_bank"], [("std-other",), ("STD-1",)])
        self.assertEqual(len(remaining["standardized_quiz_versions"]), 1)
        for table in ("quiz_attempts", "attempt_responses", "assigned_quizzes"):
            self.assertEqual(len(remaining[table]), 3)
            self.assertNotIn("0", [str(row[0]) for row in remaining[table]])
        self.assertEqual(remaining["quiz_questions_map"], [(1, "std-other"), (2, "STD-1")])
        self.connection.commit.assert_called_once()

    def test_partial_deletion_is_rolled_back_on_error_or_interrupt(self):
        before = self.snapshot()
        for error in (RuntimeError("database failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.connection.reset_mock()

                def execute(sql, params):
                    if " ".join(sql.split()).startswith("DELETE FROM standardized_quiz_versions"):
                        raise error
                    self.execute(sql, params)

                self.cursor.execute.side_effect = execute
                with self.assertRaises(type(error)):
                    self.delete()
                self.assertEqual(self.snapshot(), before)
                self.connection.rollback.assert_called_once()
                self.connection.commit.assert_not_called()

    def test_confirmation_is_exact_and_never_changes_data_when_canceled(self):
        before = self.snapshot()
        for confirmation in (f"DELETE {self.name} ", f"delete {self.name}", ""):
            with self.subTest(confirmation=confirmation):
                self.assertFalse(self.delete(confirmation))
                self.assertEqual(self.snapshot(), before)
                self.connection.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
