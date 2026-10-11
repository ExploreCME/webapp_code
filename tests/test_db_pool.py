import asyncio
import importlib.util
import os
from pathlib import Path
import ssl
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]


class DbConnectionTests(unittest.TestCase):
    def setUp(self):
        self.modules = {name: MagicMock() for name in (
            "pymysql", "dbutils", "dbutils.pooled_db", "dotenv", "aiomysql")}
        self.modules["aiomysql"].connect = AsyncMock()
        self.env = {
            "MYSQL_HOST": "managed.example.com", "MYSQL_PORT": "25060",
            "MYSQL_USER": "test-user", "MYSQL_PASSWORD": "test-password",
            "MYSQL_DB": "test-db",
        }

    def load(self, name):
        spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def settings(self, env):
        with patch.dict(sys.modules, self.modules), patch.dict(os.environ, env, clear=True):
            pool = self.load("db_pool")
            pool.get_db_connection()
            asyncio.run(pool.get_async_db_connection())
        return (self.modules["dbutils.pooled_db"].PooledDB.call_args.kwargs,
                self.modules["aiomysql"].connect.call_args.kwargs)

    def test_managed_mysql_settings_match_pool(self):
        pooled, asynchronous = self.settings(self.env)
        for key in ("host", "port", "user", "password", "charset", "connect_timeout"):
            self.assertEqual(pooled[key], asynchronous[key])
        self.assertEqual(asynchronous["port"], 25060)
        self.assertEqual(asynchronous["db"], pooled["database"])
        self.assertEqual(asynchronous["connect_timeout"], 10)
        self.assertIs(asynchronous["cursorclass"], self.modules["aiomysql"].DictCursor)
        self.assertNotIn("autocommit", asynchronous)
        self.assertIsInstance(asynchronous["ssl"], ssl.SSLContext)
        self.assertEqual(asynchronous["ssl"].verify_mode, ssl.CERT_NONE)
        self.assertFalse(asynchronous["ssl"].check_hostname)
        self.assertEqual(pooled["ssl"], {"ssl": {}})

    def test_local_mysql_defaults_and_ssl_disabled_values(self):
        for disabled in ("1", "true", "YES", "On"):
            with self.subTest(disabled=disabled):
                env = {**self.env, "MYSQL_SSL_DISABLED": disabled,
                       "MYSQL_SSL_CA": "/unused/ca.pem"}
                del env["MYSQL_PORT"]
                pooled, asynchronous = self.settings(env)
                self.assertEqual(pooled["port"], 3306)
                self.assertEqual(asynchronous["port"], 3306)
                self.assertIsNone(pooled["ssl"])
                self.assertIsNone(asynchronous["ssl"])

    def test_custom_ca_enables_certificate_and_hostname_verification(self):
        context = ssl.create_default_context()
        with patch("ssl.create_default_context", return_value=context) as create:
            pooled, asynchronous = self.settings({**self.env, "MYSQL_SSL_CA": "/test/ca.pem"})
        create.assert_called_once_with(cafile="/test/ca.pem")
        self.assertEqual(pooled["ssl"], {"ca": "/test/ca.pem"})
        self.assertIs(asynchronous["ssl"], context)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_callers_import_shared_helpers(self):
        with patch.dict(sys.modules, self.modules):
            pool = self.load("db_pool")
            with patch.dict(sys.modules, {
                "db_pool": pool,
                "Question_writing_and_verification_optomized": MagicMock(),
            }):
                on_demand = self.load("on_demand_generator")
                exporter = self.load("export_questions_to_word")
        self.assertIs(on_demand.get_db_connection, pool.get_async_db_connection)
        self.assertIs(exporter.get_db_connection, pool.get_db_connection)
        conn = MagicMock()
        conn.cursor.return_value.fetchall.return_value = []
        with patch.object(exporter, "get_db_connection", return_value=conn), \
                patch("builtins.print"):
            exporter.export_questions()
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
