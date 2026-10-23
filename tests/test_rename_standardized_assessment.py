from contextlib import redirect_stderr, redirect_stdout
import io
import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import rename_standardized_assessment as script


OLD = "Emergecy Medicine"
NEW = "Emergency Medicine"
PREFIX = script.STANDARDIZED_PREFIX


def inputs(*values):
    return MagicMock(side_effect=list(values))


class ValidateNamesTests(unittest.TestCase):
    def test_strips_and_returns_names(self):
        self.assertEqual(script.validate_names(f"  {OLD} ", f" {NEW}  "), (OLD, NEW))

    def test_new_name_uses_built_in_catalog_capitalization(self):
        self.assertEqual(script.validate_names(OLD, "emergency MEDICINE"), (OLD, NEW))

    def test_rejects_invalid_names(self):
        cases = [
            ("", NEW), (OLD, "   "), (None, NEW),
            ("Standardized: Emergecy Medicine", NEW), (OLD, "standardized:Emergency Medicine"),
            (OLD, OLD), (OLD, " Emergecy Medicine "),
            ("Emergecy Medicine version 1", NEW), (OLD, "Emergency Medicine Version 2"),
            (OLD, "Emergency\nMedicine"),
            (OLD, "x" * (255 - len(PREFIX + " version 1") + 1)),
        ]
        for old, new in cases:
            with self.subTest(old=old, new=new):
                with self.assertRaises(ValueError):
                    script.validate_names(old, new)

    def test_accepts_longest_name_that_fits(self):
        longest = "x" * (255 - len(PREFIX + " version 1"))
        self.assertEqual(script.validate_names(OLD, longest), (OLD, longest))

    def test_parse_version_is_exact(self):
        self.assertEqual(script.parse_version(f"{OLD} version 12", OLD), 12)
        for name in (f"{OLD} version 01", f"{OLD} version 1a", f"{OLD} version ",
                     f"{OLD} Advanced version 1", f"{OLD}version 1", f"{OLD} version ²"):
            with self.subTest(name=name):
                self.assertIsNone(script.parse_version(name, OLD))


class RenameDatabaseTests(unittest.TestCase):
    """Exercise the SQL against SQLite with MySQL-specific syntax translated."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE standardized_quiz_versions (
                quiz_name TEXT PRIMARY KEY COLLATE NOCASE, category TEXT COLLATE NOCASE,
                question_ids TEXT, created_date TEXT
            );
            CREATE TABLE question_bank (id TEXT PRIMARY KEY, question_stem TEXT);
            CREATE TABLE quiz_attempts (
                attempt_id TEXT PRIMARY KEY, username TEXT, quiz_name TEXT COLLATE NOCASE,
                quiz_name_norm TEXT, mode TEXT, start_time TEXT
            );
            CREATE TABLE attempt_responses (
                id INTEGER PRIMARY KEY, attempt_id TEXT, question_id TEXT,
                user_choice TEXT, is_correct INTEGER
            );
            CREATE TABLE assigned_quizzes (
                id INTEGER PRIMARY KEY, quiz_name TEXT COLLATE NOCASE, class_id INTEGER
            );
            CREATE TABLE user_quizzes (quiz_id TEXT PRIMARY KEY, username TEXT, quiz_name TEXT);
        """)
        catalog = [
            (f"{OLD} version {n}", OLD, json.dumps([f"std-{n}a", f"std-{n}b"]), f"2026-10-0{n}")
            for n in (1, 2, 3)
        ] + [
            ("Family Medicine version 1", "Family Medicine", '["std-fm"]', "2026-10-04"),
            (f"{OLD} Advanced version 1", f"{OLD} Advanced", '["std-adv"]', "2026-10-05"),
        ]
        self.db.executemany("INSERT INTO standardized_quiz_versions VALUES (?, ?, ?, ?)", catalog)
        self.db.executemany(
            "INSERT INTO question_bank VALUES (?, ?)",
            [(qid, f"Stem {qid}") for qid in
             ("std-1a", "std-1b", "std-2a", "std-2b", "std-3a", "std-3b", "std-fm", "std-adv")],
        )
        attempts = [
            ("a1", PREFIX + f"{OLD} version 1"), ("a2", PREFIX + f"{OLD} version 1"),
            ("a3", PREFIX + f"{OLD} version 2"), ("a4", PREFIX + f"{OLD} version 3"),
            ("sample", PREFIX + NEW), ("adv", PREFIX + f"{OLD} Advanced version 1"),
            ("user", f"{OLD} version 1"), ("fm", PREFIX + "Family Medicine version 1"),
        ]
        for index, (attempt_id, name) in enumerate(attempts):
            self.db.execute(
                "INSERT INTO quiz_attempts VALUES (?, ?, ?, ?, ?, ?)",
                (attempt_id, f"student{index}", name, name.strip().lower(), "test", "2026-10-10"),
            )
            self.db.execute(
                "INSERT INTO attempt_responses VALUES (?, ?, ?, ?, ?)",
                (index, attempt_id, "std-1a", "A", index % 2),
            )
        self.db.executemany(
            "INSERT INTO assigned_quizzes VALUES (?, ?, ?)",
            [(1, PREFIX + f"{OLD} version 1", 7), (2, PREFIX + f"{OLD} version 3", 7),
             (3, PREFIX + f"{OLD} Advanced version 1", 7), (4, f"{OLD} version 1", 7)],
        )
        self.db.execute("INSERT INTO user_quizzes VALUES ('q1', 'student6', ?)", (f"{OLD} version 1",))

        self.engines = {table: "InnoDB" for table in script.TABLES}
        self.norm_columns = {"quiz_attempts": "STORED"}
        self.executed = []
        self.connection = MagicMock()
        self.connection.begin.side_effect = lambda: self.db.execute("BEGIN")
        self.connection.commit.side_effect = self.db.commit
        self.connection.rollback.side_effect = self.db.rollback
        self.cursor = MagicMock()
        self.connection.cursor.return_value.__enter__.return_value = self.cursor
        self.cursor.execute.side_effect = self.execute

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), tuple(params)))
        if "information_schema.TABLES" in sql:
            rows = [{"table_name": t, "engine": e} for t, e in self.engines.items() if t in params]
            self.cursor.fetchall.side_effect = lambda: rows
            return
        if "information_schema.COLUMNS" in sql:
            rows = [{"table_name": t, "extra": e} for t, e in self.norm_columns.items()]
            self.cursor.fetchall.side_effect = lambda: rows
            return
        sql = sql.replace(" FOR UPDATE", "").replace("%s", "?")
        sql = sql.replace("BINARY quiz_name = ?", "quiz_name = ? COLLATE BINARY")
        result = self.db.execute(sql, params)
        self.cursor.rowcount = result.rowcount
        self.cursor.fetchone.side_effect = result.fetchone
        self.cursor.fetchall.side_effect = result.fetchall

    def snapshot(self):
        return {
            table: [tuple(row) for row in self.db.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("standardized_quiz_versions", "question_bank", "quiz_attempts",
                          "attempt_responses", "assigned_quizzes", "user_quizzes")
        }

    def rename(self, *answers, dry_run=False, old=OLD, new=NEW):
        output = []
        prompt = inputs(*answers)
        result = script.rename_assessment(
            self.connection, old, new, dry_run=dry_run, input_func=prompt,
            print_func=lambda *args: output.append(" ".join(map(str, args))),
        )
        return result, "\n".join(output), prompt

    def updates(self):
        return [sql for sql, _ in self.executed if sql.startswith("UPDATE")]

    def test_renames_all_versions_and_exact_references_atomically(self):
        before = self.snapshot()
        result, output, _ = self.rename(f"RENAME {OLD}")

        self.assertTrue(result)
        self.connection.commit.assert_called_once()
        after = self.snapshot()
        self.assertEqual(after["standardized_quiz_versions"], [
            (f"{OLD} Advanced version 1", f"{OLD} Advanced", '["std-adv"]', "2026-10-05"),
            (f"{NEW} version 1", NEW, json.dumps(["std-1a", "std-1b"]), "2026-10-01"),
            (f"{NEW} version 2", NEW, json.dumps(["std-2a", "std-2b"]), "2026-10-02"),
            (f"{NEW} version 3", NEW, json.dumps(["std-3a", "std-3b"]), "2026-10-03"),
            ("Family Medicine version 1", "Family Medicine", '["std-fm"]', "2026-10-04"),
        ])
        attempts = {row[0]: row for row in after["quiz_attempts"]}
        for attempt_id, number in (("a1", 1), ("a2", 1), ("a3", 2), ("a4", 3)):
            name = PREFIX + f"{NEW} version {number}"
            self.assertEqual(attempts[attempt_id][2:4], (name, name.lower()))
        old_attempts = {row[0]: row for row in before["quiz_attempts"]}
        for attempt_id in ("sample", "adv", "user", "fm"):
            self.assertEqual(attempts[attempt_id], old_attempts[attempt_id])
        for attempt_id in attempts:  # usernames, modes and start times are preserved
            self.assertEqual(attempts[attempt_id][:2] + attempts[attempt_id][4:],
                             old_attempts[attempt_id][:2] + old_attempts[attempt_id][4:])
        self.assertEqual(after["assigned_quizzes"], [
            (1, PREFIX + f"{NEW} version 1", 7), (2, PREFIX + f"{NEW} version 3", 7),
            (3, PREFIX + f"{OLD} Advanced version 1", 7), (4, f"{OLD} version 1", 7),
        ])
        for table in ("question_bank", "attempt_responses", "user_quizzes"):
            self.assertEqual(after[table], before[table])
        self.assertIn(f"'{OLD} version 2' -> '{NEW} version 2'", output)
        self.assertIn("quiz_attempts: 2, assigned_quizzes: 1", output)
        self.assertIn("Renamed 3 published version(s)", output)
        self.assertFalse(any("question_bank" in sql or "attempt_responses" in sql
                             for sql in self.updates()))

    def test_generated_norm_column_is_not_written(self):
        self.norm_columns = {"quiz_attempts": "STORED GENERATED"}
        self.assertTrue(self.rename(f"RENAME {OLD}")[0])
        self.assertTrue(self.updates())
        self.assertFalse(any("quiz_name_norm =" in sql for sql in self.updates()))

    def test_renames_any_number_of_versions_preserving_suffixes(self):
        self.db.execute("DELETE FROM standardized_quiz_versions WHERE quiz_name = ?",
                        (f"{OLD} version 2",))
        self.db.execute("INSERT INTO standardized_quiz_versions VALUES (?, ?, '[]', 'd')",
                        (f"{OLD} version 10", OLD))
        self.assertTrue(self.rename(f"RENAME {OLD}")[0])
        names = [row[0] for row in self.db.execute(
            "SELECT quiz_name FROM standardized_quiz_versions WHERE category = ? COLLATE BINARY "
            "ORDER BY quiz_name", (NEW,))]
        self.assertEqual(names, [f"{NEW} version 1", f"{NEW} version 10", f"{NEW} version 3"])

    def test_dry_run_previews_without_writes_or_prompt(self):
        before = self.snapshot()
        result, output, prompt = self.rename(dry_run=True)
        self.assertTrue(result)
        prompt.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.updates(), [])
        self.connection.commit.assert_not_called()
        self.assertIn(f"Category: '{OLD}' -> '{NEW}'", output)
        self.assertIn(f"'{OLD} version 1' -> '{NEW} version 1'", output)
        self.assertIn("Dry run: no changes were made.", output)

    def test_preview_does_not_lock_but_revalidation_does(self):
        self.rename(f"RENAME {OLD}")
        selects = [sql for sql, _ in self.executed if sql.startswith("SELECT")
                   and "information_schema" not in sql]
        self.assertEqual(len(selects), 6)
        self.assertFalse(any(sql.endswith("FOR UPDATE") for sql in selects[:3]))
        self.assertTrue(all(sql.endswith("FOR UPDATE") for sql in selects[3:]))
        self.assertFalse(any("LIKE" in sql for sql, _ in self.executed))

    def test_no_match_reports_and_writes_nothing(self):
        before = self.snapshot()
        for old in ("Emergecy", "Emergecy Medicine Adv", "Nonexistent"):
            with self.subTest(old=old):
                result, output, prompt = self.rename(old=old)
                self.assertFalse(result)
                prompt.assert_not_called()
                self.assertIn("No published standardized assessment found", output)
        self.assertEqual(self.snapshot(), before)
        self.connection.commit.assert_not_called()

    def test_cancellation_writes_nothing(self):
        before = self.snapshot()
        for answer in (f"rename {OLD}", f"RENAME {NEW}", "", "y"):
            with self.subTest(answer=answer):
                result, output, _ = self.rename(answer)
                self.assertFalse(result)
                self.assertIn("Cancelled. Nothing was renamed.", output)
        for error in (KeyboardInterrupt(), EOFError()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    script.rename_assessment(
                        self.connection, OLD, NEW,
                        input_func=MagicMock(side_effect=error), print_func=lambda *a: None,
                    )
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.updates(), [])
        self.connection.commit.assert_not_called()

    def assert_refused(self, pattern):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, pattern):
            self.rename(f"RENAME {OLD}")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.updates(), [])
        self.connection.commit.assert_not_called()

    def test_refuses_existing_destination_version(self):
        self.db.execute("INSERT INTO standardized_quiz_versions VALUES (?, ?, '[]', 'd')",
                        (f"{NEW} version 2", NEW))
        self.assert_refused("already exists")

    def test_refuses_merge_into_existing_category(self):
        self.db.execute("INSERT INTO standardized_quiz_versions VALUES (?, ?, '[]', 'd')",
                        (f"{NEW} version 9", NEW))
        self.assert_refused("already exists")

    def test_refuses_destination_attempt_and_assignment_names(self):
        for table, row in (
            ("quiz_attempts", ("x", "u", PREFIX + f"{NEW} version 1",
                               (PREFIX + f"{NEW} version 1").lower(), "test", "d")),
            ("quiz_attempts", ("x", "u", PREFIX + f"{NEW.upper()} version 3",
                               (PREFIX + f"{NEW} version 3").lower(), "test", "d")),
            ("assigned_quizzes", (9, PREFIX + f"{NEW} version 2", 7)),
        ):
            with self.subTest(table=table, row=row):
                self.db.execute(
                    f"INSERT INTO {table} VALUES ({', '.join('?' * len(row))})", row)
                self.assert_refused("already uses")
                key = "attempt_id" if table == "quiz_attempts" else "id"
                self.db.execute(f"DELETE FROM {table} WHERE {key} = ?", (row[0],))

    def test_refuses_case_variant_references_to_old_name(self):
        self.db.execute(
            "INSERT INTO quiz_attempts VALUES ('v', 'u', ?, ?, 'test', 'd')",
            ((PREFIX + f"{OLD} version 1").lower(), (PREFIX + f"{OLD} version 1").lower()),
        )
        self.assert_refused("variant")

    def test_refuses_stale_norm_column(self):
        self.db.execute(
            "INSERT INTO quiz_attempts VALUES ('v', 'u', 'Something else', ?, 'test', 'd')",
            ((PREFIX + f"{NEW} version 1").lower(),),
        )
        self.assert_refused("out of sync")

    def test_refuses_inconsistent_catalog_metadata(self):
        for name, category in ((f"{OLD} version 4", "Other"), ("Odd name", OLD),
                               ("emergecy medicine version 5", "emergecy medicine")):
            with self.subTest(name=name):
                self.db.execute(
                    "INSERT INTO standardized_quiz_versions VALUES (?, ?, '[]', 'd')",
                    (name, category))
                self.assert_refused("Inconsistent published metadata")
                self.db.execute(
                    "DELETE FROM standardized_quiz_versions WHERE quiz_name = ? COLLATE BINARY",
                    (name,))

    def test_refuses_reserved_sample_names(self):
        with patch.dict(script.STANDARDIZED_QUIZZES, {f"{NEW} version 2": []}):
            self.assert_refused("reserved")

    def test_refuses_non_transactional_tables(self):
        self.engines["assigned_quizzes"] = "MyISAM"
        self.assert_refused("InnoDB")
        del self.engines["assigned_quizzes"]
        self.assert_refused("InnoDB")

    def test_failure_at_each_update_rolls_back_everything(self):
        before = self.snapshot()
        for table in ("standardized_quiz_versions", "quiz_attempts", "assigned_quizzes"):
            for error in (RuntimeError("update failed"), KeyboardInterrupt()):
                with self.subTest(table=table, error=type(error).__name__):
                    self.connection.reset_mock()
                    calls = []

                    def execute(sql, params=()):
                        if " ".join(sql.split()).startswith(f"UPDATE {table} "):
                            calls.append(sql)
                            if len(calls) == 2:
                                raise error
                        self.execute(sql, params)

                    self.cursor.execute.side_effect = execute
                    with self.assertRaises(type(error)):
                        self.rename(f"RENAME {OLD}")
                    self.assertEqual(self.snapshot(), before)
                    self.connection.rollback.assert_called()
                    self.connection.commit.assert_not_called()

    def test_unexpected_rowcount_rolls_back(self):
        before = self.snapshot()

        def execute(sql, params=()):
            self.execute(sql, params)
            if sql.startswith("UPDATE quiz_attempts"):
                self.cursor.rowcount = 0

        self.cursor.execute.side_effect = execute
        with self.assertRaisesRegex(RuntimeError, "rolling back"):
            self.rename(f"RENAME {OLD}")
        self.assertEqual(self.snapshot(), before)
        self.connection.commit.assert_not_called()

    def test_changes_after_preview_abort_without_writes(self):
        before = self.snapshot()

        def confirm(_prompt):
            self.db.execute(
                "INSERT INTO quiz_attempts VALUES ('late', 'u', ?, ?, 'test', 'd')",
                (PREFIX + f"{OLD} version 1", (PREFIX + f"{OLD} version 1").lower()),
            )
            return f"RENAME {OLD}"

        with self.assertRaisesRegex(RuntimeError, "changed after the preview"):
            script.rename_assessment(self.connection, OLD, NEW,
                                     input_func=confirm, print_func=lambda *a: None)
        self.db.execute("DELETE FROM quiz_attempts WHERE attempt_id = 'late'")
        self.db.execute("DELETE FROM attempt_responses WHERE attempt_id = 'late'")
        self.assertEqual(self.snapshot(), before)
        self.connection.commit.assert_not_called()

    def test_case_only_rename_is_supported(self):
        self.db.execute("UPDATE standardized_quiz_versions SET quiz_name = REPLACE(quiz_name, ?, ?), "
                        "category = ? WHERE category = ? COLLATE BINARY",
                        (OLD, NEW.lower(), NEW.lower(), OLD))
        self.db.execute("UPDATE quiz_attempts SET quiz_name = REPLACE(quiz_name, ?, ?) "
                        "WHERE quiz_name LIKE ?", (OLD, NEW.lower(), f"%{OLD} version%"))
        self.db.execute("UPDATE assigned_quizzes SET quiz_name = REPLACE(quiz_name, ?, ?) "
                        "WHERE quiz_name LIKE ?", (OLD, NEW.lower(), f"%{OLD} version%"))
        self.assertTrue(self.rename(f"RENAME {NEW.lower()}", old=NEW.lower())[0])
        self.assertEqual(
            self.db.execute("SELECT quiz_name FROM quiz_attempts WHERE attempt_id = 'a1'")
            .fetchone()[0], PREFIX + f"{NEW} version 1")


class MainTests(unittest.TestCase):
    def test_validation_happens_before_connecting(self):
        connect = MagicMock()
        for argv in (["--old-name", "", "--new-name", NEW],
                     ["--old-name", OLD, "--new-name", OLD],
                     ["--old-name", OLD, "--new-name", f"{PREFIX}{NEW}"]):
            with self.subTest(argv=argv):
                with self.assertRaises(ValueError):
                    script.main(argv, input_func=MagicMock(), print_func=lambda *a: None,
                                connect=connect)
        connect.assert_not_called()

    def test_arguments_and_prompts_are_passed_and_connection_closed(self):
        connection = MagicMock()
        with patch.object(script, "rename_assessment", return_value=True) as rename:
            prompt = inputs(f" {OLD} ", NEW)
            self.assertTrue(script.main(["--dry-run"], input_func=prompt,
                                        print_func=print, connect=lambda: connection))
            self.assertEqual(prompt.call_count, 2)
            self.assertEqual(rename.call_args.args[1:], (OLD, NEW))
            self.assertTrue(rename.call_args.kwargs["dry_run"])
            connection.close.assert_called_once()

            prompt = MagicMock()
            script.main(["--old-name", OLD, "--new-name", NEW], input_func=prompt,
                        connect=lambda: connection)
            prompt.assert_not_called()
            self.assertFalse(rename.call_args.kwargs["dry_run"])

    def test_connection_closed_on_error(self):
        connection = MagicMock()
        with patch.object(script, "rename_assessment", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                script.main(["--old-name", OLD, "--new-name", NEW], connect=lambda: connection)
        connection.close.assert_called_once()

    def test_run_exit_codes(self):
        for outcome, expected in ((True, 0), (False, 1)):
            with patch.object(script, "main", return_value=outcome):
                self.assertEqual(script.run([]), expected)
        for error in (KeyboardInterrupt(), EOFError()):
            with patch.object(script, "main", side_effect=error):
                with redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(script.run([]), 1)
                self.assertIn("Cancelled", stdout.getvalue())
        with patch.object(script, "main", side_effect=ValueError("bad name")):
            with redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(script.run([]), 1)
        self.assertIn("Failed: bad name", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
