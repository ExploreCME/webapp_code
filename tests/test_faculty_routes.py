import unittest
from html import escape
from unittest.mock import MagicMock, patch
from urllib.parse import quote

with patch("os.makedirs"):
    import display_database

import faculty_routes


class FacultyReviewRouteTests(unittest.TestCase):
    quiz_names = ("normal quiz", "10/17 test", "10/17 test? #1 & 50%")

    def setUp(self):
        self.app = display_database.app
        config = self.app.config.copy()
        self.addCleanup(lambda: self.app.config.update(config))
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.client.set_cookie("__session", "test-clerk-token")
        self.auth_db = MagicMock()
        self.auth_db.cursor.return_value.__enter__.return_value = self.auth_db.cursor.return_value
        self.auth_db.cursor.return_value.fetchone.return_value = {
            "has_paid": 1, "is_faculty": "Y",
        }
        self.db = MagicMock()
        self.cursor = self.db.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = {"quiz_id": 42}
        self.cursor.fetchall.return_value = []
        for patcher in (
            patch.object(display_database, "MAINTENANCE_MODE", False),
            patch.object(display_database, "decode_clerk_token", return_value={"sub": "faculty-user"}),
            patch.object(display_database, "get_db_connection", return_value=self.auth_db),
            patch.object(faculty_routes, "get_mysql_connection", return_value=self.db),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def review_url(self, quiz_name):
        return "/faculty/review/" + quote(quiz_name, safe="/&")

    def assert_owner_lookup(self, quiz_name):
        query, params = self.cursor.execute.call_args_list[0].args
        self.assertEqual(params, (quiz_name, "faculty-user"))
        self.assertIn("quiz_name = %s", query)
        self.assertIn("TRIM(LOWER(", query)
        self.assertIn("username", query)

    def test_review_page_and_add_questions_link(self):
        for quiz_name in self.quiz_names:
            with self.subTest(quiz_name=quiz_name):
                self.cursor.reset_mock()
                url = self.review_url(quiz_name)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assert_owner_lookup(quiz_name)
                self.assertIn(
                    f'href="{escape(url + "/add_questions", quote=True)}"',
                    response.get_data(as_text=True),
                )

    def test_review_list_links_preserve_quiz_names(self):
        self.cursor.fetchall.return_value = [
            {"quiz_name": name, "question_count": 0} for name in self.quiz_names
        ]
        response = self.client.get("/faculty/review")
        self.assertEqual(response.status_code, 200)
        for quiz_name in self.quiz_names:
            self.assertIn(
                f'href="{escape(self.review_url(quiz_name), quote=True)}"',
                response.get_data(as_text=True),
            )

    def test_add_questions_page_and_search_form(self):
        for quiz_name in self.quiz_names:
            with self.subTest(quiz_name=quiz_name):
                url = self.review_url(quiz_name) + "/add_questions"
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    f'action="{escape(url, quote=True)}"',
                    response.get_data(as_text=True),
                )

    def test_search_results_and_attach_form(self):
        for quiz_name in self.quiz_names:
            with self.subTest(quiz_name=quiz_name):
                self.cursor.reset_mock()
                self.cursor.fetchall.side_effect = [[], [{"id": 7}]]
                url = self.review_url(quiz_name)
                response = self.client.post(url + "/add_questions", data={"num_questions": "1"})
                self.assertEqual(response.status_code, 200)
                self.assert_owner_lookup(quiz_name)
                self.assertIn(
                    f'action="{escape(url + "/attach_questions", quote=True)}"',
                    response.get_data(as_text=True),
                )
                self.db.commit.assert_not_called()

    def test_remediation_details_use_owner_scoped_source_labels_and_module_ids(self):
        labels = ["OB - Prenatal care/normal pregnancy", "OB - Pregnancy complications"]
        self.cursor.fetchall.return_value = [
            dict(id=f"module-{i}", remediation_name="Upload",
                 organ_system="Reproductive System", source_organ_system=label,
                 task_area="Diagnosis", topic_area="Topic")
            for i, label in enumerate(labels)
        ]
        response = self.client.get("/faculty/review_remediation/Upload")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for i, label in enumerate(labels):
            self.assertIn(label, html)
            self.assertIn(f"/remediation/review/module-{i}", html)
            self.assertIn(f"/faculty/review_remediation_module/module-{i}", html)
        self.assertNotIn("Reproductive System", html)
        query, params = self.cursor.execute.call_args_list[0].args
        self.assertIn("source_organ_system", query)
        self.assertIn("WHERE username_norm = LOWER(TRIM(%s))", query)
        self.assertEqual(params, ("faculty-user",))

    def test_remediation_question_review_preserves_source_label_and_owner_lookup(self):
        self.cursor.fetchone.return_value = {
            "id": "module-1", "remediation_name": "Upload",
            "organ_system": "Reproductive System",
            "source_organ_system": "OB - Prenatal care/normal pregnancy",
        }
        response = self.client.get("/faculty/review_remediation_module/module-1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("OB - Prenatal care/normal pregnancy", response.get_data(as_text=True))
        query, params = self.cursor.execute.call_args_list[0].args
        self.assertIn("source_organ_system", query)
        self.assertIn("WHERE id = %s AND username_norm = LOWER(TRIM(%s))", query)
        self.assertEqual(params, ("module-1", "faculty-user"))
    def test_attach_questions_and_review_redirect(self):
        for quiz_name in self.quiz_names:
            with self.subTest(quiz_name=quiz_name):
                self.db.reset_mock()
                url = self.review_url(quiz_name)
                response = self.client.post(
                    url + "/attach_questions", data={"selected_questions": ["7", "8"]},
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, url)
                self.assert_owner_lookup(quiz_name)
                self.cursor.executemany.assert_called_once_with(
                    "INSERT IGNORE INTO quiz_questions_map (quiz_id, question_id) VALUES (%s, %s)",
                    [(42, "7"), (42, "8")],
                )
                self.db.commit.assert_called_once()

    def test_review_routes_reject_unauthenticated_and_non_faculty(self):
        for authenticated in (False, True):
            for quiz_name in self.quiz_names:
                for suffix, method in (("", "GET"), ("/add_questions", "GET"),
                                       ("/add_questions", "POST"), ("/attach_questions", "POST")):
                    with self.subTest(authenticated=authenticated, quiz_name=quiz_name,
                                      suffix=suffix, method=method):
                        if authenticated:
                            self.client.set_cookie("__session", "test-clerk-token")
                        else:
                            self.client.delete_cookie("__session")
                        self.auth_db.cursor.return_value.fetchone.return_value["is_faculty"] = "N"
                        self.cursor.fetchone.return_value = {"is_faculty": "N"}
                        self.db.reset_mock()
                        response = self.client.open(
                            self.review_url(quiz_name) + suffix, method=method,
                            data={"selected_questions": ["7"]},
                        )
                        self.assertEqual(response.status_code, 302)
                        self.assertEqual(response.location, "/paxcel")
                        self.cursor.fetchall.assert_not_called()
                        self.cursor.executemany.assert_not_called()
                        self.db.commit.assert_not_called()
                        if not authenticated:
                            self.db.cursor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
