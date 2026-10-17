import io
import json
import unittest
from unittest.mock import mock_open, patch

from flask import Flask, url_for
from pymysql.err import OperationalError, ProgrammingError
from pypdf import PdfReader

import flask_app
import student_reports
from standardized_quizzes import STANDARDIZED_PREFIX
from test_standardized_quizzes import QuizDatabase


class StudentReportTests(unittest.TestCase):
    def setUp(self):
        self.db = QuizDatabase()
        self.addCleanup(self.db.connection.close)
        self.app = Flask(__name__, template_folder="../templates/template standardization")
        self.app.config.update(TESTING=True, SECRET_KEY="report-test-only")
        self.app.register_blueprint(student_reports.student_reports_bp)
        self.app.add_url_rule("/", "paxcel_landing", lambda: "Sign in")
        self.app.add_url_rule("/eula", "quiz.view_eula", lambda: "EULA")
        self.client = self.app.test_client()
        with self.client.session_transaction() as login:
            login.update(username="student", accepted_eula=True)
        self.db_patch = patch.object(flask_app, "get_db", return_value=self.db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.open_patch = patch("report_engine.open", mock_open(), create=True)
        self.debug_open = self.open_patch.start()
        self.addCleanup(self.open_patch.stop)
        for sql in (
            "ALTER TABLE users ADD COLUMN accepted_eula INTEGER DEFAULT 1",
            "ALTER TABLE remediation_questions ADD COLUMN assignment_id INTEGER",
            "ALTER TABLE remediation_assignments ADD COLUMN id INTEGER",
            "ALTER TABLE remediation_assignments ADD COLUMN organ_system TEXT",
            "ALTER TABLE remediation_assignments ADD COLUMN task_area TEXT",
            "ALTER TABLE remediation_assignments ADD COLUMN topic_area TEXT",
        ):
            self.db.execute(sql)
        for qid, organ, task, topic in (
            ("q1", "Cardiovascular System", "Clinical Intervention", "Resuscitation"),
            ("q2", "Pulmonary System", "Clinical Intervention", "Breathing"),
            ("q3", "Endocrine System", "Clinical Intervention", "Diabetes"),
            ("outsider", "Renal System", "Clinical Intervention", "Other"),
        ):
            self.db.execute(
                "INSERT INTO question_bank (id, organ_system, task_area, topic_area) VALUES (%s,%s,%s,%s)",
                (qid, organ, task, topic),
            )
        self.version("Version One", ["q1", "q2"])
        self.version("Version Two", ["q3"])

    def version(self, name, ids, category="Emergency Medicine"):
        self.db.execute("INSERT INTO standardized_quiz_versions VALUES (%s,%s,%s,%s)",
                        (name, category, json.dumps(ids), "2026-10-01"))

    def attempt(self, aid, answers, user="student", version="Version One",
                timestamp="2026-10-10", mode="test"):
        name = STANDARDIZED_PREFIX + version
        self.db.execute("INSERT INTO quiz_attempts VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (aid, user, user.strip().lower(), name, name.lower(), mode, timestamp))
        for qid, correct in answers.items():
            self.db.execute("""
                INSERT INTO attempt_responses
                (attempt_id, question_id, user_choice, is_correct, submitted_time)
                VALUES (%s,%s,%s,%s,%s)
            """, (aid, qid, "A", correct, timestamp))

    def sections(self, attempt_filter="recent"):
        return student_reports.standardized_report(
            self.db, "student", "Emergency Medicine", attempt_filter,
        )

    def test_exact_membership_completion_latest_tie_and_modes(self):
        self.attempt("a", {"q1": 1, "q2": 1})
        self.attempt("b", {"q1": 1, "q2": 0}, mode="study_no_exp")
        self.attempt("z", {"q1": 1, "outsider": 1}, timestamp="2026-10-15")
        self.attempt("other-version", {"q3": 0}, version="Version Two")
        sections = self.sections()
        self.assertEqual([a["attempt_id"] for a in sections[0]["attempts"]], ["b"])
        self.assertEqual(sections[0]["student"], student_reports.metric(1, 2))
        self.assertEqual(sections[1]["student"], student_reports.metric(0, 1))
        self.assertEqual(self.sections("all")[0]["student"], student_reports.metric(3, 4))

    def test_platform_weighting_independent_of_filter_and_no_raw_histories(self):
        self.attempt("a", {"q1": 1, "q2": 1})
        self.attempt("b", {"q1": 1, "q2": 0})
        self.attempt("private-user-attempt", {"q1": 0, "q2": 0}, user="private-user")
        self.attempt("incomplete", {"q1": 1}, user="private-user", timestamp="2026-10-16")
        with patch.object(self.db, "execute", wraps=self.db.execute) as queries:
            recent = self.sections()[0]
        all_attempts = self.sections("all")[0]
        self.assertEqual(recent["platform"], student_reports.metric(3, 6))
        self.assertEqual(recent["platform"], all_attempts["platform"])
        self.assertEqual(recent["available_completed_attempts"], 2)
        self.assertEqual(recent["platform_attempt_count"], 3)
        platform_sql = [
            call.args[0] for call in queries.call_args_list if "SELECT ar.question_id" in call.args[0]
        ][0]
        self.assertIn("GROUP BY ar.question_id", platform_sql)
        self.assertNotIn("username", platform_sql)
        page = self.client.get("/student_reports/preview", query_string={
            "report_type": "standardized", "category": "Emergency Medicine",
            "username": "private-user", "student_id": "private-user",
        })
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("private-user", page.text)
        self.assertNotIn("private-user-attempt", page.text)
        self.assertIn("50.0%", page.text)
        self.assertIn("Your percentage correct", page.text)
        self.assertIn("Platform percentage correct", page.text)

    def test_recent_is_sql_limited_and_catalog_questions_are_batched(self):
        self.attempt("a", {"q1": 1, "q2": 1})
        self.attempt("b", {"q1": 0, "q2": 0})
        with patch.object(self.db, "execute", wraps=self.db.execute) as statements:
            sections = self.sections()
        queries = [call.args[0] for call in statements.call_args_list]
        self.assertEqual(sum("SELECT quiz_name, question_ids" in query for query in queries), 1)
        self.assertEqual(sum("FROM question_bank" in query for query in queries), 1)
        selected_queries = [query for query in queries if "ORDER BY qa.start_time" in query]
        self.assertEqual(len(selected_queries), 2)
        self.assertTrue(all(query.endswith(" LIMIT 1") for query in selected_queries))
        self.assertEqual(sections[0]["available_completed_attempts"], 2)
        self.assertEqual(len(sections[0]["attempts"]), 1)

    def test_all_dimensions_no_observations_and_version_isolation(self):
        self.attempt("different", {"q1": 1, "q2": 1}, version="Version Two")
        sections = self.sections()
        self.assertIsNone(sections[0]["student"]["percentage"])
        self.assertIsNone(sections[1]["platform"]["percentage"])
        self.assertEqual(len(sections[0]["groups"]), 2)
        self.assertEqual([row["label"] for row in sections[0]["groups"][0]["rows"]],
                         ["Cardiovascular System", "Pulmonary System"])
        self.assertEqual([row["label"] for row in sections[0]["groups"][1]["rows"]],
                         ["Clinical Intervention"])
        page = self.client.get("/student_reports/preview?report_type=standardized&category=Emergency+Medicine")
        self.assertEqual(page.text.count('<section class="report-section">'), 2)
        self.assertIn("N/A", page.text)
        self.assertIn("No completed attempts", page.text)
        self.assertIn("Samples are excluded", page.text)

    def test_empty_published_membership_is_visible_and_unobserved(self):
        self.version("Empty Version", [])
        sections = self.sections()
        self.assertEqual([section["title"] for section in sections],
                         ["Empty Version", "Version One", "Version Two"])
        empty = sections[0]
        self.assertEqual(empty["question_count"], 0)
        self.assertEqual(empty["available_completed_attempts"], 0)
        self.assertEqual(empty["platform_attempt_count"], 0)
        self.assertIsNone(empty["student"]["percentage"])
        self.assertIsNone(empty["platform"]["percentage"])
        self.assertEqual(empty["groups"], [
            {"title": "Organ Systems", "rows": []}, {"title": "Task Areas", "rows": []},
        ])
        page = self.client.get("/student_reports/preview?report_type=standardized&category=Emergency+Medicine")
        self.assertIn("Empty Version", page.text)
        self.assertIn("Catalog questions: 0", page.text)
        self.assertIn("No questions in this published version.", page.text)
        pdf = self.client.get("/student_reports/download", query_string={
            "report_type": "standardized", "category": "Emergency Medicine",
        })
        self.assertIn("No questions in this published version.",
                      PdfReader(io.BytesIO(pdf.data)).pages[0].extract_text())
        pdf.close()

    def test_completion_uses_recorded_membership_not_completion_column_or_extra_rows(self):
        self.attempt("valid", {"q1": 1, "q2": 0})
        self.attempt("incomplete", {"q1": 1}, timestamp="2026-10-16")
        self.attempt("extra-row", {"q1": 1, "outsider": 1}, timestamp="2026-10-16")
        sections = self.sections("all")
        self.assertEqual([row["attempt_id"] for row in sections[0]["attempts"]], ["valid"])
        self.assertEqual(sections[0]["student"], student_reports.metric(1, 2))
        self.assertEqual(sections[0]["platform"], student_reports.metric(1, 2))
        self.assertEqual(self.sections("recent")[0]["student"], student_reports.metric(1, 2))

    def test_usage_calls_faculty_engine_scoped_only_to_session_and_includes_all_modes(self):
        self.attempt("own-test", {"q1": 1})
        self.attempt("own-study", {"q2": 0}, mode="study_no_exp")
        self.attempt("other", {"q1": 0}, user="other")
        original = student_reports.ReportEngine.generate_overall_usage
        with patch.object(student_reports.ReportEngine, "generate_overall_usage",
                          autospec=True, side_effect=original) as engine:
            response = self.client.get("/student_reports/preview?username=other&target_student_id=other")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(engine.call_args.args[1], ["student"])
        self.assertEqual(engine.call_args.kwargs, {"write_debug": False})
        self.debug_open.assert_not_called()
        self.assertIn("50.0%", response.text)
        self.assertIn("1 / 2 correct / eligible", response.text)
        self.assertIn("incomplete attempts", response.text)

    def test_downloads_are_real_pdf_and_versions_have_document_page_break(self):
        self.attempt("a", {"q1": 1, "q2": 0})
        for kind in ("usage", "standardized"):
            with self.subTest(kind=kind), patch.object(
                student_reports, "PageBreak", wraps=student_reports.PageBreak,
            ) as page_break:
                response = self.client.get("/student_reports/download", query_string={
                    "report_type": kind, "category": "Emergency Medicine",
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "application/pdf")
                self.assertTrue(response.data.startswith(b"%PDF-"))
                self.assertIn("attachment;", response.headers["Content-Disposition"])
                self.assertIn("private, no-store", response.headers["Cache-Control"])
                self.assertEqual(page_break.call_count, 1 if kind == "standardized" else 0)
                pages = [page.extract_text() for page in PdfReader(io.BytesIO(response.data)).pages]
                if kind == "standardized":
                    version_pages = [
                        index for index, text in enumerate(pages)
                        if "Version One" in text or "Version Two" in text
                    ]
                    self.assertEqual(len(version_pages), 2)
                    self.assertNotEqual(*version_pages)
                    self.assertIn("Version Two", pages[version_pages[1]])
                else:
                    self.assertIn("Overall Usage", pages[0])
                    self.assertIn("Organ Systems", "".join(pages))
                response.close()

    def test_hub_preview_download_links_and_escaped_catalog_labels(self):
        self.version("<script>alert(1)</script>", ["q1"])
        with self.app.test_request_context():
            self.assertEqual(url_for("student_reports.reports"), "/student_reports")
        hub = self.client.get("/student_reports")
        self.assertEqual(hub.status_code, 200)
        self.assertIn("Overall Usage", hub.text)
        self.assertIn('<option value="standardized">Standardized Assessment</option>', hub.text)
        self.assertIn("Most recent attempt", hub.text)
        self.assertIn("Aggregate of all completed attempts", hub.text)
        self.assertIn("max-width: 900px", hub.text)
        self.assertIn('formaction="/student_reports/download"', hub.text)
        page = self.client.get("/student_reports/preview", query_string={
            "report_type": "standardized", "category": "Emergency Medicine",
        })
        self.assertIn("&lt;script&gt;", page.text)
        self.assertNotIn("<script>alert(1)</script>", page.text)

    def test_auth_and_validation_reject_before_database(self):
        paths = ("/student_reports", "/student_reports/preview",
                 "/student_reports/download")
        for user in (None, "guest_example", "GUEST_example"):
            with self.client.session_transaction() as login:
                login.clear()
                if user:
                    login["username"] = user
            for path in paths:
                self.assertEqual(self.client.get(path).status_code, 403)
        with self.client.session_transaction() as login:
            login["username"] = "student"
        with patch.object(flask_app, "get_db") as connection:
            for query in (
                {"report_type": "other"}, {"attempt_filter": "highest"},
                {"report_type": "standardized"},
            ):
                self.assertEqual(self.client.get("/student_reports/preview", query_string=query).status_code, 400)
            connection.assert_not_called()
        with patch.object(self.db, "execute", wraps=self.db.execute) as statements:
            response = self.client.get("/student_reports/preview", query_string={
                "report_type": "standardized", "category": "' OR 1=1 --",
            })
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("' OR 1=1 --", statements.call_args.args[0])
        self.assertEqual(statements.call_args.args[1], ("' OR 1=1 --",))

    def test_eula_reuses_existing_interceptor_for_report_routes(self):
        self.client.set_cookie("__session", "verified-session-token")
        with self.client.session_transaction() as login:
            login["accepted_eula"] = False
        self.db.execute("UPDATE users SET accepted_eula = 0")
        with patch.object(flask_app.jwt, "decode", return_value={"sub": "student"}):
            for path in ("/student_reports", "/student_reports/preview", "/student_reports/download"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/eula")
            self.db.execute("UPDATE users SET accepted_eula = 1")
            self.assertEqual(self.client.get("/student_reports").status_code, 200)
        with self.client.session_transaction() as login:
            self.assertTrue(login["accepted_eula"])

    def test_published_custom_category_selectable_reported_and_aggregate_labeled(self):
        self.version("Custom Exam", ["q1"], category="Custom Specialty")
        self.attempt("custom", {"q1": 1}, version="Custom Exam")
        hub = self.client.get("/student_reports")
        self.assertIn('<option value="Custom Specialty">Custom Specialty</option>', hub.text)
        options = {
            "report_type": "standardized", "category": "Custom Specialty",
            "attempt_filter": "all",
        }
        preview = self.client.get("/student_reports/preview", query_string=options)
        self.assertEqual(preview.status_code, 200)
        self.assertIn("Custom Exam", preview.text)
        self.assertIn("Average score:", preview.text)
        self.assertNotIn("Version One", preview.text)
        pdf = self.client.get("/student_reports/download", query_string=options)
        self.assertEqual(pdf.status_code, 200)
        text = PdfReader(io.BytesIO(pdf.data)).pages[0].extract_text()
        self.assertIn("Average score:", text)
        self.assertIn("Custom Exam", text)
        pdf.close()

    def test_category_filter_and_missing_schema_no_ddl(self):
        self.version("Family Version", ["q1"], category="Family Medicine")
        self.assertEqual(len(self.sections()), 2)
        self.db.execute("DROP TABLE standardized_quiz_versions")
        with self.assertLogs("standardized_quizzes", level="WARNING"), patch.object(
            self.db, "execute", wraps=self.db.execute,
        ) as statements:
            self.assertEqual(self.sections(), [])
        self.assertFalse(any("CREATE" in call.args[0] for call in statements.call_args_list))
        for error in (ProgrammingError(1054, "unknown column"), OperationalError(2006, "gone")):
            with patch.object(self.db, "execute", side_effect=error):
                with self.assertRaises(type(error)):
                    self.sections()

    def test_missing_catalog_question_fails_instead_of_changing_denominator(self):
        self.db.execute("DELETE FROM question_bank WHERE id = 'q2'")
        with self.assertRaisesRegex(ValueError, "missing questions"):
            self.sections()

    def test_production_hooks_trust_verified_identity_and_close_request_connection(self):
        # Import the real app to exercise its Clerk/session bridge and blueprint wiring.
        with patch("os.makedirs"):
            import display_database
        app = display_database.app
        config = app.config.copy()
        self.addCleanup(lambda: app.config.update(config))
        app.config.update(TESTING=True, SECRET_KEY="report-production-test")
        client = app.test_client()
        client.set_cookie("__session", "verified-token")
        with client.session_transaction() as login:
            login.update(username="spoofed-user", accepted_eula=True)
        self.attempt("a", {"q1": 1, "q2": 0})
        self.db_patch.stop()
        with patch.object(display_database, "MAINTENANCE_MODE", False), \
                patch.object(display_database, "decode_clerk_token", return_value={"sub": "student"}), \
                patch.object(display_database, "get_db_connection", return_value=self.db), \
                patch.object(flask_app, "get_db_connection", return_value=self.db), \
                patch.object(self.db, "close", wraps=self.db.close) as close:
            for path in ("/student_reports", "/student_reports/preview", "/student_reports/download"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()
            self.assertGreaterEqual(close.call_count, 5)
            with client.session_transaction() as login:
                self.assertEqual(login["username"], "student")


if __name__ == "__main__":
    unittest.main()
