import ast
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, call, patch


ROOT = Path(__file__).resolve().parents[1]


class MySQLError(Exception):
    pass


class RemediationSchemaTests(unittest.TestCase):
    def setUp(self):
        self.mysql = MagicMock(MySQLError=MySQLError)
        self.db_pool = MagicMock()
        with patch.dict(sys.modules, {
            "pymysql": self.mysql, "dotenv": MagicMock(), "db_pool": self.db_pool,
        }):
            spec = importlib.util.spec_from_file_location("init_db", ROOT / "init.db.py")
            self.init = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.init)
        self.connection = self.db_pool.get_db_connection.return_value
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.return_value = (1,)
        self.cursor.fetchall.return_value = [
            ("remediation_assignments",), ("remediation_questions",)]

    def conversions(self):
        return [args[0] for args, _ in self.cursor.execute.call_args_list
                if "CONVERT TO CHARACTER SET" in args[0]]

    def test_converts_both_foreign_key_tables_and_restores_checks(self):
        for previous in (0, 1):
            with self.subTest(previous=previous):
                self.cursor.reset_mock()
                self.cursor.fetchone.return_value = (previous,)

                self.init.migrate_remediation_utf8mb4(self.cursor)

                self.assertEqual(self.conversions(), [
                    f"ALTER TABLE {table} CONVERT TO CHARACTER SET utf8mb4 "
                    "COLLATE utf8mb4_unicode_ci"
                    for table in ("remediation_assignments", "remediation_questions")])
                self.assertEqual(
                    self.cursor.execute.call_args,
                    call("SET SESSION FOREIGN_KEY_CHECKS = %s", (previous,)))

    def test_completed_migration_is_a_no_op(self):
        self.cursor.fetchall.return_value = []

        self.init.migrate_remediation_utf8mb4(self.cursor)

        self.assertEqual(self.cursor.execute.call_count, 1)
        query = self.cursor.execute.call_args.args[0]
        self.assertIn("t.TABLE_SCHEMA = DATABASE()", query)
        self.assertIn("t.TABLE_COLLATION <> 'utf8mb4_unicode_ci'", query)
        self.assertIn("c.COLLATION_NAME <> 'utf8mb4_unicode_ci'", query)
        self.cursor.fetchone.assert_not_called()
        self.assertEqual(self.conversions(), [])

    def test_partial_migration_only_converts_remaining_table(self):
        self.cursor.fetchall.return_value = [("remediation_questions",)]

        self.init.migrate_remediation_utf8mb4(self.cursor)

        self.assertEqual(self.conversions(), [
            "ALTER TABLE remediation_questions CONVERT TO CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_unicode_ci"])

    def test_conversion_failure_warns_and_restores_checks(self):
        def execute(sql, *args):
            if sql.startswith("ALTER TABLE"):
                raise MySQLError(1142, "ALTER denied")

        self.cursor.execute.side_effect = execute
        with patch("builtins.print") as output:
            self.init.migrate_remediation_utf8mb4(self.cursor)

        self.assertIn("migration incomplete", output.call_args.args[0])
        self.assertEqual(self.cursor.execute.call_args,
                         call("SET SESSION FOREIGN_KEY_CHECKS = %s", (1,)))

    def test_init_adds_missing_explanation_and_runs_migration(self):
        env = {key: "test" for key in (
            "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB")}
        with patch.dict(os.environ, env), patch("builtins.print"):
            self.init.init_mysql()

        self.db_pool.get_db_connection.assert_called_once_with()
        self.mysql.connect.assert_not_called()
        self.connection.cursor.assert_called_once_with(self.mysql.cursors.Cursor)
        statements = [args[0] for args, _ in self.cursor.execute.call_args_list]
        self.assertIn(
            "ALTER TABLE remediation_assignments ADD COLUMN diagram_explanation "
            "TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci", statements)
        self.assertEqual(len(self.conversions()), 2)
        child_creation = next(i for i, sql in enumerate(statements)
                              if "CREATE TABLE IF NOT EXISTS remediation_questions" in sql)
        conversions = [i for i, sql in enumerate(statements)
                       if "CONVERT TO CHARACTER SET" in sql]
        self.assertTrue(all(i < child_creation for i in conversions))
        self.connection.commit.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_duplicate_explanation_column_is_safe_on_repeated_init(self):
        def execute(sql, *args):
            if "ADD COLUMN diagram_explanation" in sql:
                raise MySQLError(1060, "Duplicate column")

        self.cursor.execute.side_effect = execute
        self.cursor.fetchall.return_value = []
        env = {key: "test" for key in (
            "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB")}
        with patch.dict(os.environ, env), patch("builtins.print") as output:
            self.init.init_mysql()
            self.init.init_mysql()

        self.assertFalse(any("⚠️" in str(args) for args, _ in output.call_args_list))
        self.assertEqual(self.conversions(), [])
        self.assertEqual(self.connection.close.call_count, 2)

    def test_both_schema_definitions_support_utf8mb4(self):
        for path in ("init.db.py", "flask_app.py10011137"):
            with self.subTest(path=path):
                tree = ast.parse((ROOT / path).read_text())
                statements = [node.value for node in ast.walk(tree)
                              if isinstance(node, ast.Constant)
                              and isinstance(node.value, str)]
                for table in ("remediation_assignments", "remediation_questions"):
                    definitions = [sql for sql in statements
                                   if f"CREATE TABLE IF NOT EXISTS {table} (" in sql]
                    self.assertEqual(len(definitions), 1)
                    self.assertIn(
                        "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
                        definitions[0])
                    if table == "remediation_assignments":
                        for column in ("summary", "diagram_code", "diagram_explanation"):
                            self.assertIn(
                                f"{column} TEXT CHARACTER SET utf8mb4 "
                                "COLLATE utf8mb4_unicode_ci", definitions[0])


if __name__ == "__main__":
    unittest.main()
