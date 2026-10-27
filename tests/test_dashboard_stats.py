import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from cachelib import SimpleCache

import standardizers

with patch("os.makedirs"):
    import display_database


class DashboardStatsTests(unittest.TestCase):
    def setUp(self):
        self.app = display_database.app
        config = self.app.config.copy()
        self.addCleanup(lambda: self.app.config.update(config))
        self.app.config.update(TESTING=True)
        cache_patch = patch.dict(
            self.app.extensions["cache"], {display_database.cache: SimpleCache()}
        )
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

        self.sqlite = sqlite3.connect(":memory:")
        self.addCleanup(self.sqlite.close)
        self.sqlite.row_factory = sqlite3.Row
        self.sqlite.execute(
            "CREATE TABLE question_bank "
            "(id INTEGER PRIMARY KEY, organ_system TEXT COLLATE NOCASE, task_area TEXT)"
        )
        self.sql_cursor = self.sqlite.cursor()
        self.db = MagicMock()
        self.cursor = self.db.cursor.return_value
        self.cursor.execute.side_effect = self.execute
        self.cursor.fetchone.side_effect = self.sql_cursor.fetchone
        self.cursor.fetchall.side_effect = self.sql_cursor.fetchall
        db_patch = patch.object(display_database, "get_mysql_db", return_value=self.db)
        self.get_db = db_patch.start()
        self.addCleanup(db_patch.stop)

    def execute(self, query, params):
        # SQLite equivalent of MySQL's byte-exact comparison and placeholders.
        query = query.replace("BINARY organ_system", "organ_system COLLATE BINARY")
        return self.sql_cursor.execute(query.replace("%s", "?"), params)

    def insert(self, rows):
        self.sqlite.executemany(
            "INSERT INTO question_bank (organ_system, task_area) VALUES (?, ?)", rows
        )

    def fetch(self):
        with self.app.app_context():
            return display_database.fetch_data_for_dashboard()

    def test_all_authoritative_organs_are_reused_without_top_n_limit(self):
        self.assertIs(display_database.ORGAN_SYSTEMS, standardizers.ORGAN_SYSTEMS)
        self.insert((organ, "Clinical Intervention") for organ in standardizers.ORGAN_SYSTEMS)
        total, organs, tasks = self.fetch()
        self.assertEqual(total, len(standardizers.ORGAN_SYSTEMS))
        self.assertEqual(dict(organs), dict.fromkeys(standardizers.ORGAN_SYSTEMS, 1))
        self.assertEqual(tasks, [("Clinic Int.", total)])
        self.assertEqual(self.cursor.execute.call_count, 3)
        for call in self.cursor.execute.call_args_list:
            query, params = call.args
            self.assertTrue(query.lstrip().startswith("SELECT"))
            self.assertIn("BINARY organ_system IN (", query)
            self.assertEqual(query.count("%s"), len(standardizers.ORGAN_SYSTEMS))
            self.assertIs(params, standardizers.ORGAN_SYSTEMS)

    def test_totals_organs_and_tasks_use_the_same_population(self):
        self.insert([
            ("Cardiovascular System", "Clinical Intervention"),
            ("Cardiovascular System", "Clinic Int."),
            ("Professional Practice", "History Taking and Performing Physical Examination"),
            ("Misc", "History Taking and Performing Physical Examination"),
            ("Renal System", None),
            ("Renal System", ""),
        ])
        excluded = (
            "Neurology/Developmental", "Pulmonary/Thoracic Surgery",
            "Trauma/Acute Care", "Pediatrics", "Diabetes mellitus",
            "Cardiovascular System: Heart failure", "unknown category",
            "", "   ", None,
        )
        self.insert((organ, "Excluded top task") for organ in excluded for _ in range(10))
        total, organs, tasks = self.fetch()
        self.assertEqual(total, 6)
        self.assertEqual(dict(organs), {
            "Cardiovascular System": 2, "Professional Practice": 1,
            "Misc": 1, "Renal System": 2,
        })
        self.assertEqual(dict(tasks), {"Clinic Int.": 2, "Hx & PE": 2})
        self.assertEqual(total, sum(count for _, count in organs))

    def test_case_and_whitespace_variants_are_not_exact_stored_categories(self):
        canonical = "Cardiovascular System"
        self.insert((label, "Clinical Intervention") for label in (
            canonical, canonical.lower(), canonical.upper(),
            " " + canonical, canonical + " ", canonical + "\t",
            canonical.replace(" ", "  "), "Mísc",
        ))
        self.assertEqual(self.fetch(), (1, [(canonical, 1)], [("Clinic Int.", 1)]))

    def test_empty_and_only_ineligible_data(self):
        self.assertEqual(self.fetch(), (0, [], []))
        display_database.cache.clear()
        self.insert([(None, "Clinical Intervention"), ("custom", "Clinical Intervention")])
        self.assertEqual(self.fetch(), (0, [], []))

    def test_task_abbreviations_sorting_and_seven_item_contract(self):
        self.insert([
            ("Misc", task)
            for index, task in enumerate(standardizers.TASK_AREAS)
            for _ in range(index + 2)
        ] + [("Misc", "Other task")])
        _, _, tasks = self.fetch()
        self.assertEqual(tasks, [
            ("Basic Science", 8), ("Pharm Therapeutics", 7), ("Clinic Int.", 6),
            ("Health Maint & Prev", 5), ("Most Likely Dx", 4),
            ("Labs & Dx", 3), ("Hx & PE", 2),
        ])

    def test_legacy_cache_is_ignored_and_filtered_result_is_cached_for_300_seconds(self):
        display_database.cache.set("dashboard_stats", (999, [("custom", 999)], []))
        self.insert([("Misc", "Clinical Intervention")])
        with patch.object(
            display_database.cache.cache, "set", wraps=display_database.cache.cache.set
        ) as cache_set:
            expected = (1, [("Misc", 1)], [("Clinic Int.", 1)])
            self.assertEqual(self.fetch(), expected)
            self.insert([("Misc", "Clinical Intervention")])
            self.assertEqual(self.fetch(), expected)
        self.get_db.assert_called_once()
        self.assertEqual(self.cursor.execute.call_count, 3)
        self.assertEqual(cache_set.call_args.kwargs["timeout"], 300)
        self.assertNotEqual(cache_set.call_args.args[0], "dashboard_stats")

    def test_connection_and_each_query_error_return_empty_fallback(self):
        with patch.object(display_database, "get_mysql_db", side_effect=RuntimeError("offline")):
            self.assertEqual(self.fetch(), (0, [], []))
        for failing_query in range(3):
            with self.subTest(failing_query=failing_query):
                display_database.cache.clear()
                with patch.object(
                    self.cursor, "execute",
                    side_effect=[None] * failing_query + [RuntimeError("query failed")],
                ):
                    self.assertEqual(self.fetch(), (0, [], []))

    def test_missing_count_row_returns_zero(self):
        with patch.object(self.cursor, "fetchone", side_effect=None, return_value=None):
            self.assertEqual(self.fetch(), (0, [], []))

    def test_landing_and_dashboard_render_filtered_data_with_request_scoped_auth(self):
        self.insert([("Misc", "Clinical Intervention"), ("custom", "Excluded task")])
        auth_db = MagicMock()
        auth_db.cursor.return_value.__enter__.return_value.fetchone.return_value = {
            "has_paid": 1, "is_faculty": "Y",
        }
        with patch.object(display_database, "MAINTENANCE_MODE", False), \
                patch.object(display_database, "get_db_connection", return_value=auth_db), \
                patch.object(display_database, "decode_clerk_token", return_value={"sub": "dashboard-user"}) as decode, \
                patch.object(display_database, "render_template", wraps=display_database.render_template) as render:
            client = self.app.test_client()
            client.set_cookie("__session", "test-clerk-token")
            for path in ("/paxcel", "/db_dashboard", "/paxcel"):
                with self.subTest(path=path):
                    response = client.get(path, headers={"User-Agent": "median"})
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("no-store", response.headers["Cache-Control"])
                    render.assert_called_with(
                        "index_for_display.html", total_questions=1,
                        organ_labels=["Misc"], organ_counts=[1],
                        task_labels=["Clinic Int."], task_counts=[1],
                    )
                    self.assertIn('data-target="1"', response.text)
                    self.assertIn("@clerk/clerk-js", response.text)
            self.assertEqual(decode.call_count, 3)
            self.assertEqual(auth_db.close.call_count, 3)
            self.get_db.assert_called_once()

    def test_native_login_dashboard_auth_and_maintenance_skip_stats(self):
        with patch.object(display_database, "MAINTENANCE_MODE", False), \
                patch.object(display_database, "get_db_connection") as auth_db, \
                patch.object(display_database, "render_template", wraps=display_database.render_template) as render:
            client = self.app.test_client()
            self.assertEqual(client.get("/paxcel", headers={"User-Agent": "median"}).status_code, 200)
            render.assert_called_with("app_login.html")
            response = client.get("/db_dashboard")
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith("/paxcel"))
            with patch.object(display_database, "MAINTENANCE_MODE", True):
                self.assertEqual(client.get("/paxcel").status_code, 503)
                render.assert_called_with("maintenance.html")
            auth_db.assert_not_called()
            self.get_db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
