import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE_COLUMN = (
    "source_organ_system TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL"
)


class MySQLError(Exception):
    pass


class StandardizedSourceSchemaTests(unittest.TestCase):
    def setUp(self):
        self.mysql = MagicMock(MySQLError=MySQLError)
        pool = MagicMock()
        with patch.dict(sys.modules, {
            "pymysql": self.mysql, "dotenv": MagicMock(), "db_pool": pool,
        }):
            spec = importlib.util.spec_from_file_location("init_db_source", ROOT / "init.db.py")
            self.init = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.init)
        self.connection = pool.get_db_connection.return_value
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchall.return_value = []

    def initialize(self):
        with patch.dict(os.environ, {key: "test" for key in (
            "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB",
        )}), patch("builtins.print"):
            self.init.init_mysql()

    def test_fresh_and_existing_schema_keep_nullable_provenance(self):
        self.initialize()
        statements = [call.args[0] for call in self.cursor.execute.call_args_list]
        creation = next(sql for sql in statements
                        if "CREATE TABLE IF NOT EXISTS question_bank (" in sql)
        self.assertIn(SOURCE_COLUMN, creation)
        self.assertIn("ALTER TABLE question_bank ADD COLUMN " + SOURCE_COLUMN, statements)
        migration = (ROOT / "migrations/20261026_standardized_source_heading.sql").read_text()
        self.assertIn("information_schema.COLUMNS", migration)
        self.assertIn("ALTER TABLE question_bank ADD COLUMN " + SOURCE_COLUMN, migration)
        self.assertNotIn("UPDATE question_bank", migration)
        self.connection.commit.assert_called_once()

    def test_duplicate_column_is_safe_but_other_migration_errors_propagate(self):
        for code in (1060, 1142):
            with self.subTest(code=code):
                self.connection.reset_mock()

                def execute(sql, *args):
                    if sql.startswith("ALTER TABLE question_bank ADD COLUMN source_organ_system"):
                        raise MySQLError(code, "Schema error")

                self.cursor.execute.side_effect = execute
                if code == 1060:
                    self.initialize()
                    self.connection.commit.assert_called_once()
                else:
                    with self.assertRaises(MySQLError):
                        self.initialize()
                    self.connection.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
