import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

from flask import Blueprint


ROOT = Path(__file__).resolve().parents[1]


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HomepagePremiumRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modules = {
            name: MagicMock()
            for name in ("pymysql", "stripe", "dotenv", "db_pool")
        }
        for name, attr in (("flask_app", "quiz_bp"), ("user_stats", "stats_bp"),
                           ("faculty_routes", "faculty_bp"),
                           ("preview_app", "preview_bp")):
            modules[name] = MagicMock(**{attr: Blueprint(attr, name)})
        env = {"MAINTENANCE_MODE": "False", "FLASK_ENV": ""}
        with patch.dict(sys.modules, modules), patch.dict(os.environ, env):
            cls.module = load_module("display_database")
        cls.module.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)

    def setUp(self):
        self.client = self.module.app.test_client()

    def get_index(self, db_row):
        conn = MagicMock()
        conn.cursor.return_value.fetchone.return_value = db_row
        with patch.object(self.module, "decode_clerk_token",
                          return_value={"sub": "user_123"}), \
                patch.object(self.module, "get_db_connection", return_value=conn), \
                patch.object(self.module, "fetch_data_for_dashboard",
                             return_value=(0, [], [])), \
                patch.object(self.module, "render_template", return_value="ok"):
            self.client.set_cookie(key="__session", value="token")
            self.client.get("/")
        return conn

    def test_premium_status_refreshed_despite_recent_sync(self):
        with self.client.session_transaction() as sess:
            sess["is_premium"] = False
            sess["last_db_sync"] = 9999999999

        conn = self.get_index({"has_paid": 1, "is_faculty": "N"})

        conn.cursor.return_value.execute.assert_called_once()
        self.assertEqual(conn.cursor.return_value.execute.call_args.args[1], ("user_123",))
        with self.client.session_transaction() as sess:
            self.assertTrue(sess["is_premium"])
            self.assertFalse(sess["is_faculty"])

    def test_missing_user_is_not_premium(self):
        with self.client.session_transaction() as sess:
            sess["is_premium"] = True

        self.get_index(None)

        with self.client.session_transaction() as sess:
            self.assertFalse(sess["is_premium"])
            self.assertFalse(sess["is_faculty"])


if __name__ == "__main__":
    unittest.main()
