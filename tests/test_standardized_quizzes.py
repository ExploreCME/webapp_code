import sqlite3
import json
import re
import unittest
from html import unescape
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from flask import Flask, render_template, session, url_for
from pymysql.err import DataError, OperationalError, ProgrammingError

import flask_app
from student_reports import student_reports_bp
from standardized_quizzes import STANDARDIZED_PREFIX, STANDARDIZED_QUIZZES, store_standardized_questions


class QuizDatabase:
    """Exercise shared quiz SQL locally, translating only MySQL-specific syntax."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.create_function("JSON_LENGTH", 1, lambda value: len(json.loads(value)))
        self.connection.row_factory = lambda cursor, row: dict(
            zip((column[0] for column in cursor.description), row)
        )
        self.connection.executescript("""
            CREATE TABLE users (clerk_id TEXT, has_paid INTEGER, is_faculty TEXT, program_code TEXT);
            INSERT INTO users VALUES ('student', 1, 'N', 'NONE');
            CREATE TABLE class_members (clerk_id TEXT, class_id INTEGER);
            CREATE TABLE remediation_assignments (remediation_name TEXT);
            CREATE TABLE remediation_questions (
                id TEXT, question_stem TEXT, answer_choice_A TEXT, answer_choice_B TEXT,
                answer_choice_C TEXT, answer_choice_D TEXT, answer_choice_E TEXT, explanation TEXT
            );
            CREATE TABLE assigned_quizzes (
                quiz_name TEXT, clerk_id TEXT, class_id INTEGER, program_code TEXT,
                due_date TEXT, required_mode TEXT, assigned_date TEXT, assigned_by TEXT
            );
            CREATE TABLE assigned_remediations (
                remediation_name TEXT, clerk_id TEXT, class_id INTEGER, program_code TEXT,
                due_date TEXT, assigned_by TEXT
            );
            CREATE TABLE user_quizzes (quiz_id TEXT, username TEXT, quiz_name TEXT, created_date TEXT);
            CREATE TABLE standardized_quiz_versions (
                quiz_name TEXT PRIMARY KEY, category TEXT, question_ids TEXT, created_date TEXT
            );
            CREATE TABLE quiz_questions_map (map_id INTEGER, quiz_id TEXT, question_id TEXT);
            CREATE TABLE question_bank (
                id TEXT PRIMARY KEY, organ_system TEXT, task_area TEXT, topic_area TEXT,
                question_stem TEXT, answer_choice_A TEXT, answer_choice_B TEXT,
                answer_choice_C TEXT, answer_choice_D TEXT, answer_choice_E TEXT,
                correct_answer TEXT, explanation TEXT, difficulty_rating INTEGER, created_date TEXT,
                global_thumbs_up INTEGER DEFAULT 0, global_thumbs_down INTEGER DEFAULT 0
            );
            CREATE TABLE quiz_attempts (
                attempt_id TEXT PRIMARY KEY, username TEXT, username_norm TEXT,
                quiz_name TEXT, quiz_name_norm TEXT, mode TEXT, start_time TEXT
            );
            CREATE TABLE attempt_responses (
                id INTEGER PRIMARY KEY, attempt_id TEXT, question_id TEXT, user_choice TEXT,
                correct_answer TEXT, is_correct INTEGER, submitted_time TEXT,
                UNIQUE (attempt_id, question_id)
            );
        """)

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def begin(self):
        self.connection.execute("BEGIN")

    def rollback(self):
        self.connection.rollback()

    def close(self):
        pass

    def translate(self, query):
        query = query.replace("%s", "?").replace("NOW()", "CURRENT_TIMESTAMP")
        query = query.replace("INSERT IGNORE", "INSERT OR IGNORE")
        if "ON DUPLICATE KEY UPDATE" in query:
            query = query.split("ON DUPLICATE KEY UPDATE")[0] + """
                ON CONFLICT (attempt_id, question_id) DO UPDATE SET
                user_choice = excluded.user_choice, correct_answer = excluded.correct_answer,
                is_correct = excluded.is_correct, submitted_time = excluded.submitted_time
            """
        return query

    def execute(self, query, parameters=()):
        try:
            self.result = self.connection.execute(self.translate(query), parameters)
        except sqlite3.OperationalError as error:
            if str(error) == "no such table: standardized_quiz_versions":
                raise ProgrammingError(
                    1146, "Table 'test.standardized_quiz_versions' doesn't exist"
                ) from error
            raise

    def executemany(self, query, parameters):
        self.connection.executemany(self.translate(query), parameters)

    def fetchone(self):
        return self.result.fetchone()

    def fetchall(self):
        return self.result.fetchall()

    def commit(self):
        self.connection.commit()


class StandardizedQuizTests(unittest.TestCase):
    def setUp(self):
        self.db = QuizDatabase()
        self.addCleanup(self.db.connection.close)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="standardized-test-only")
        self.app.register_blueprint(flask_app.quiz_bp)
        self.app.register_blueprint(student_reports_bp)
        self.app.add_url_rule("/", "paxcel_landing", lambda: "Sign in")
        self.app.add_url_rule("/statistics", "stats.my_statistics", lambda: "Statistics")
        self.client = self.app.test_client()
        self.login()
        db_patch = patch.object(flask_app, "get_db", return_value=self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        self.quiz_name = STANDARDIZED_PREFIX + "Emergency Medicine"

    def login(self, username="student"):
        with self.client.session_transaction() as session:
            session["username"] = username
            session["accepted_eula"] = True

    def token(self):
        with self.client.session_transaction() as session:
            return session.get("standardized_csrf_token", "")

    def start(self, mode="test", quiz_name=None):
        quiz_name = quiz_name or self.quiz_name
        setup = self.client.get("/quiz_mode", query_string={"quiz_name": quiz_name})
        self.assertEqual(setup.status_code, 200)
        return self.client.post("/start_quiz", data={
            "quiz_name": quiz_name, "mode": mode, "standardized_csrf_token": self.token(),
        })

    def answer(self, attempt_id, choice="A", mode="test", question=None):
        return self.client.post("/submit-answer", data={
            "quiz_name": self.quiz_name, "attempt_id": attempt_id, "mode": mode,
            "question_id": question or STANDARDIZED_QUIZZES["Emergency Medicine"][0]["id"],
            "user_choice": choice, "standardized_csrf_token": self.token(),
        })

    def publish_version(self):
        first = STANDARDIZED_QUIZZES["Emergency Medicine"][0]
        second = dict(first, id="std-emergency-second", question_stem="Second published question")
        store_standardized_questions(self.db, [second, first])
        self.db.connection.execute(
            "INSERT INTO standardized_quiz_versions VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            ("Emergency Medicine version 1", "Emergency Medicine",
             json.dumps([first["id"], second["id"]])),
        )
        self.db.commit()
        self.quiz_name = STANDARDIZED_PREFIX + "Emergency Medicine version 1"
        return first, second

    def resume_from_page(self, page, attempt_id):
        forms = re.findall(r"<form\b[^>]*>(.*?)</form>", page.text, re.DOTALL)
        form = next(form for form in forms if f'value="{attempt_id}"' in form)
        data = {
            name: unescape(value)
            for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form)
        }
        return self.client.post("/start_quiz", data=data)

    def test_dashboard_link_and_seven_category_tiles(self):
        with self.app.test_request_context():
            session["username"] = "student"
            dashboard = render_template("student_dashboard.html", is_premium=True)
        header = dashboard.split('<nav class="navbar">', 1)[1].split("</nav>", 1)[0]
        self.assertNotIn("Standardized Quizzes", header)
        self.assertNotIn("/select_standardized_quiz", header)
        self.assertIn('href="/student_dashboard"', header)
        self.assertIn("Manage Subscription", header)
        self.assertEqual(dashboard.count('href="/select_standardized_quiz"'), 1)
        self.assertIn('href="/select_standardized_quiz"', dashboard)
        self.assertIn("Standardized Assessment", dashboard)
        self.assertNotIn("Standardized Quizzes", dashboard)
        self.assertIn('href="/student_reports"', dashboard)
        response = self.client.get("/select_standardized_quiz")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertEqual(page.count('class="quiz-card"'), 7)
        self.assertEqual(list(STANDARDIZED_QUIZZES), [
            "Emergency Medicine", "Family Medicine", "Internal Medicine",
            "Pediatrics", "Behavioral Health", "Women's Health", "Surgery",
        ])
        for category in STANDARDIZED_QUIZZES:
            self.assertIn(category.replace("'", "&#39;"), page)
        self.assertIn(".quiz-grid", page)
        self.assertEqual(page.count("View Versions"), 7)
        self.assertIn("/standardized_quizzes/Emergency%20Medicine", page)

    def test_missing_versions_table_keeps_category_and_sample_selection_working(self):
        self.db.connection.execute("DROP TABLE standardized_quiz_versions")
        for path in ("/select_standardized_quiz", "/standardized_quizzes/Emergency%20Medicine"):
            with self.subTest(path=path), self.assertLogs("standardized_quizzes", level="WARNING") as logs:
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("migrations/20261016_add_standardized_quiz_versions.sql", logs.output[0])
                self.assertNotIn("test.standardized_quiz_versions", "\n".join(logs.output))
                self.assertNotIn("standardized_quiz_versions", response.text)
        self.assertIn("Try Sample Practice", response.text)
        self.assertEqual(self.start().status_code, 302)
        self.assertIsNone(self.db.connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'standardized_quiz_versions'"
        ).fetchone())

    def test_dashboard_to_selection_with_production_app_hooks(self):
        with patch("os.makedirs"):
            import display_database

        app = display_database.app
        config = app.config.copy()
        self.addCleanup(lambda: app.config.update(config))
        app.config.update(TESTING=True, SECRET_KEY="standardized-test-only")
        client = app.test_client()
        client.set_cookie("__session", "test-clerk-token")
        with client.session_transaction() as login_session:
            login_session.update(username="student", accepted_eula=True)
        with patch.object(display_database, "MAINTENANCE_MODE", False), \
                patch.object(display_database, "decode_clerk_token", return_value={"sub": "student"}), \
                patch.object(display_database, "get_db_connection", return_value=self.db):
            dashboard = client.get("/student_dashboard")
            self.assertEqual(dashboard.status_code, 200)
            self.assertEqual(dashboard.text.count('href="/select_standardized_quiz"'), 1)
            self.assertIn("Standardized Assessment", dashboard.text)
            self.assertIn('href="/student_reports"', dashboard.text)
            reports = client.get("/student_reports")
            self.assertEqual(reports.status_code, 200)
            self.assertIn("Overall Usage", reports.text)
            self.assertIn("Standardized Assessment", reports.text)
            response = client.get("/select_standardized_quiz")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text.count("View Versions"), 7)
            self.assertEqual(client.get(
                "/standardized_quizzes/Emergency%20Medicine"
            ).status_code, 200)

    def test_published_versions_list_and_setup_with_real_templates(self):
        questions = STANDARDIZED_QUIZZES["Emergency Medicine"]
        store_standardized_questions(self.db, questions)
        name = "Emergency Medicine version 1"
        self.db.connection.execute(
            "INSERT INTO standardized_quiz_versions VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (name, "Emergency Medicine", json.dumps([q["id"] for q in questions])),
        )
        self.db.commit()
        response = self.client.get("/select_standardized_quiz")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<strong>Published versions:</strong> 1", response.text)
        response = self.client.get("/standardized_quizzes/Emergency%20Medicine")
        self.assertEqual(response.status_code, 200)
        self.assertIn(name, response.text)
        self.assertIn("<strong>Questions:</strong> 1", response.text)
        self.assertNotIn("Try Sample Practice", response.text)
        self.assertEqual(self.start(quiz_name=STANDARDIZED_PREFIX + name).status_code, 302)

    def test_surgery_empty_state_and_published_version_flow(self):
        self.assertEqual(list(STANDARDIZED_QUIZZES)[6], "Surgery")
        self.assertEqual(STANDARDIZED_QUIZZES["Surgery"], [])
        response = self.client.get("/standardized_quizzes/Surgery")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No generated versions are available yet", response.text)
        self.assertNotIn("Try Sample Practice", response.text)

        questions = STANDARDIZED_QUIZZES["Emergency Medicine"]
        store_standardized_questions(self.db, questions)
        name = "Surgery version 1"
        self.db.connection.execute(
            "INSERT INTO standardized_quiz_versions VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (name, "Surgery", json.dumps([q["id"] for q in questions])),
        )
        self.db.commit()
        response = self.client.get("/standardized_quizzes/Surgery")
        self.assertEqual(response.status_code, 200)
        self.assertIn(name, response.text)
        self.assertNotIn("No generated versions", response.text)
        self.quiz_name = STANDARDIZED_PREFIX + name
        response = self.start()
        self.assertEqual(response.status_code, 302)
        attempt_id = parse_qs(urlparse(response.location).query)["attempt_id"][0]
        result = self.answer(attempt_id, question=questions[0]["id"])
        self.assertIn("100.0%", self.client.get(result.location).text)

    def test_catalog_does_not_swallow_other_database_errors(self):
        for error in (
            ProgrammingError(1142, "SELECT denied"),
            ProgrammingError(1054, "Unknown column"),
            ProgrammingError(1064, "SQL syntax error"),
            ProgrammingError(),
            OperationalError(2003, "Connection unavailable"),
            DataError(3141, "Invalid JSON"),
            ValueError("Malformed catalog data"),
        ):
            for path in ("/select_standardized_quiz", "/standardized_quizzes/Emergency%20Medicine"):
                with self.subTest(error=error, path=path), \
                        patch.object(self.db, "execute", side_effect=error), \
                        self.assertRaises(type(error)) as raised:
                    self.client.get(path)
                self.assertIs(raised.exception, error)

    def test_every_category_uses_shared_setup_take_scoring_and_review(self):
        for category, questions in STANDARDIZED_QUIZZES.items():
            with self.subTest(category=category):
                self.quiz_name = STANDARDIZED_PREFIX + category
                if not questions:
                    page = self.client.get("/standardized_quizzes/" + category)
                    self.assertEqual(page.status_code, 200)
                    self.assertIn("No generated versions are available yet", page.text)
                    self.assertNotIn("Try Sample Practice", page.text)
                    setup = self.client.get("/quiz_mode", query_string={"quiz_name": self.quiz_name})
                    self.assertIn("/select_standardized_quiz", setup.location)
                    continue
                response = self.start()
                self.assertEqual(response.status_code, 302)
                attempt_id = parse_qs(urlparse(response.location).query)["attempt_id"][0]
                page = self.client.get(response.location)
                self.assertEqual(page.status_code, 200)
                self.assertIn(questions[0]["question_stem"], page.get_data(as_text=True))
                result = self.answer(attempt_id, question=questions[0]["id"])
                self.assertIn("/quiz_results?", result.location)
                html = self.client.get(result.location).get_data(as_text=True)
                self.assertIn("100.0%", html)
                self.assertIn("/select_standardized_quiz", html)
                review = self.client.get("/review_attempt", query_string={"attempt_id": attempt_id})
                self.assertEqual(review.status_code, 200)
                self.assertIn(questions[0]["explanation"], review.get_data(as_text=True))
        count = self.db.connection.execute("SELECT COUNT(*) AS n FROM user_quizzes").fetchone()
        self.assertEqual(count["n"], 0)

    def test_all_setup_modes_and_small_question_count(self):
        setup = self.client.get("/quiz_mode", query_string={"quiz_name": self.quiz_name})
        html = setup.get_data(as_text=True)
        for mode in ("test", "study_no_exp", "study_exp", "study_missed"):
            self.assertIn(f'value="{mode}"', html)
            self.assertEqual(self.start(mode).status_code, 302)
        self.assertIn("All 1 available question(s)", html)
        self.assertIn('href="/select_standardized_quiz"', html)

    def test_study_feedback_missed_filter_and_resume(self):
        response = self.start("study_exp")
        attempt_id = parse_qs(urlparse(response.location).query)["attempt_id"][0]
        wrong = self.answer(attempt_id, choice="B", mode="study_exp")
        self.assertIn("answered=True", wrong.location)
        self.assertIn("Incorrect.", self.client.get(wrong.location).get_data(as_text=True))
        response = self.start("study_missed")
        self.assertEqual(self.client.get(response.location).status_code, 200)
        setup = self.client.get("/quiz_mode", query_string={"quiz_name": self.quiz_name})
        self.assertIn("Resume Quiz", setup.get_data(as_text=True))
        rows = self.db.connection.execute("SELECT COUNT(*) AS n FROM quiz_attempts").fetchone()["n"]
        resume = self.client.post("/start_quiz", data={
            "quiz_name": self.quiz_name, "mode": "study_exp", "resume_attempt_id": attempt_id,
            "standardized_csrf_token": self.token(),
        })
        self.assertEqual(resume.status_code, 302)
        self.assertEqual(self.client.get(resume.location).status_code, 200)
        self.assertEqual(self.db.connection.execute(
            "SELECT COUNT(*) AS n FROM quiz_attempts"
        ).fetchone()["n"], rows)
        self.assertEqual(self.db.connection.execute(
            "SELECT COUNT(*) AS n FROM question_bank"
        ).fetchone()["n"], 1)

    def test_no_missed_questions_returns_history_and_history_can_start(self):
        response = self.start("study_missed")
        redirect = self.client.get(response.location)
        self.assertIn("/quiz_attempts?", redirect.location)
        history = self.client.get(redirect.location)
        self.assertEqual(history.status_code, 200)
        self.assertIn('name="standardized_csrf_token"', history.get_data(as_text=True))

    def test_resume_selects_next_unanswered_question(self):
        first = STANDARDIZED_QUIZZES["Emergency Medicine"][0]
        second = dict(first, id="std-emergency-sample-2", question_stem="Second sample question")
        with patch.dict(STANDARDIZED_QUIZZES, {"Emergency Medicine": [first, second]}):
            response = self.start("study_exp")
            attempt_id = parse_qs(urlparse(response.location).query)["attempt_id"][0]
            self.answer(attempt_id, mode="study_exp")
            page = self.client.get(response.location).get_data(as_text=True)
            self.assertIn("Second sample question", page)
            self.assertIn("Question <strong>2</strong> of <strong>2</strong>", page)

    def test_published_quiz_resume_from_selection_setup_and_history(self):
        first, second = self.publish_version()
        selection_path = "/standardized_quizzes/Emergency%20Medicine"
        self.assertNotIn("Resume Quiz", self.client.get(selection_path).text)
        for mode in ("test", "study_no_exp", "study_exp"):
            with self.subTest(mode=mode):
                started = self.start(mode)
                attempt_id = parse_qs(urlparse(started.location).query)["attempt_id"][0]
                self.answer(attempt_id, mode=mode, question=first["id"])
                with self.client.session_transaction() as login_session:
                    login_session.clear()
                self.login()
                self.assertEqual(self.client.get("/select_standardized_quiz").status_code, 200)
                selection = self.client.get(selection_path)
                self.assertIn("Resume Quiz", selection.text)
                resume_url = unescape(re.search(
                    r'href="([^"]*/resume_quiz/[^"]+)"', selection.text,
                ).group(1))
                resumed = self.client.get(resume_url)
                for entry in ("selection", "setup", "history"):
                    with self.subTest(entry=entry):
                        if entry != "selection":
                            path = "/quiz_mode" if entry == "setup" else "/quiz_attempts"
                            page = self.client.get(path, query_string={"quiz_name": self.quiz_name})
                            self.assertIn("Resume", page.text)
                            resumed = self.resume_from_page(page, attempt_id)
                        self.assertEqual(resumed.status_code, 302)
                        query = parse_qs(urlparse(resumed.location).query)
                        self.assertEqual(query["attempt_id"], [attempt_id])
                        self.assertEqual(query["mode"], [mode])
                        question = self.client.get(resumed.location)
                        self.assertEqual(question.status_code, 200)
                        self.assertIn(second["question_stem"], question.text)
                        self.assertIn("Question <strong>2</strong> of <strong>2</strong>", question.text)
                self.answer(attempt_id, mode=mode, question=second["id"])
                self.assertNotIn("Resume Quiz", self.client.get(selection_path).text)
                history = self.client.get("/quiz_attempts", query_string={"quiz_name": self.quiz_name})
                self.assertNotIn("Resume Attempt", history.text)
                self.assertIn("Review Attempt", history.text)
                self.assertEqual(self.client.get(
                    "/review_attempt", query_string={"attempt_id": attempt_id},
                ).status_code, 200)
        self.assertEqual(self.db.connection.execute(
            "SELECT COUNT(*) AS n FROM quiz_attempts",
        ).fetchone()["n"], 3)

    def test_standardized_resume_skips_completed_and_other_students_attempts(self):
        first, second = self.publish_version()
        started = self.start("study_exp")
        incomplete_id = parse_qs(urlparse(started.location).query)["attempt_id"][0]
        self.answer(incomplete_id, mode="study_exp", question=first["id"])
        self.db.connection.execute(
            "UPDATE quiz_attempts SET start_time = '2026-01-01' WHERE attempt_id = ?",
            (incomplete_id,),
        )
        completed = self.start()
        completed_id = parse_qs(urlparse(completed.location).query)["attempt_id"][0]
        self.answer(completed_id, question=first["id"])
        self.answer(completed_id, question=second["id"])
        self.db.connection.execute("INSERT INTO users VALUES ('other', 1, 'N', 'NONE')")
        self.login("other")
        self.assertNotIn("Resume Quiz", self.client.get(
            "/standardized_quizzes/Emergency%20Medicine",
        ).text)
        other = self.start()
        other_id = parse_qs(urlparse(other.location).query)["attempt_id"][0]
        self.client.get("/quiz_mode", query_string={"quiz_name": self.quiz_name})
        self.assertEqual(self.client.post("/start_quiz", data={
            "quiz_name": self.quiz_name, "mode": "study_exp",
            "resume_attempt_id": incomplete_id, "standardized_csrf_token": self.token(),
        }).status_code, 404)
        self.login()
        with self.app.test_request_context():
            resume_url = url_for("quiz.resume_quiz", quiz_name=self.quiz_name)
        resumed = self.client.get(resume_url)
        self.assertEqual(parse_qs(urlparse(resumed.location).query)["attempt_id"], [incomplete_id])
        self.assertIn(second["question_stem"], self.client.get(resumed.location).text)
        self.answer(incomplete_id, mode="study_exp", question=second["id"])
        no_incomplete = self.client.get(resume_url)
        self.assertIn("/quiz_mode?", no_incomplete.location)
        self.assertNotIn(other_id, no_incomplete.location)

    def test_sample_quiz_selection_and_history_offer_resume_before_first_answer(self):
        started = self.start()
        attempt_id = parse_qs(urlparse(started.location).query)["attempt_id"][0]
        selection = self.client.get("/standardized_quizzes/Emergency%20Medicine")
        self.assertIn("Resume Quiz", selection.text)
        history = self.client.get("/quiz_attempts", query_string={"quiz_name": self.quiz_name})
        self.assertIn("Resume Attempt", history.text)
        resumed = self.resume_from_page(history, attempt_id)
        self.assertEqual(resumed.status_code, 302)
        self.assertEqual(self.client.get(resumed.location).status_code, 200)
        self.publish_version()
        self.assertIn("Resume Quiz — Legacy Sample Practice", self.client.get(
            "/standardized_quizzes/Emergency%20Medicine",
        ).text)

    def test_standardized_resume_returns_to_skipped_question(self):
        first, second = self.publish_version()
        started = self.start()
        attempt_id = parse_qs(urlparse(started.location).query)["attempt_id"][0]
        self.answer(attempt_id, question=second["id"])
        with self.app.test_request_context():
            resume_url = url_for("quiz.resume_quiz", quiz_name=self.quiz_name)
        resumed = self.client.get(resume_url)
        page = self.client.get(resumed.location)
        self.assertIn(first["question_stem"], page.text)
        self.assertIn("Question <strong>1</strong> of <strong>2</strong>", page.text)

    def test_completed_standardized_missed_subset_is_reviewable_not_resumable(self):
        first, second = self.publish_version()
        started = self.start()
        original_id = parse_qs(urlparse(started.location).query)["attempt_id"][0]
        self.answer(original_id, choice="B", question=first["id"])
        self.answer(original_id, question=second["id"])
        missed = self.start("study_missed")
        missed_id = parse_qs(urlparse(missed.location).query)["attempt_id"][0]
        selection_path = "/standardized_quizzes/Emergency%20Medicine"
        self.assertIn("Resume Quiz", self.client.get(selection_path).text)
        setup = self.client.get("/quiz_mode", query_string={"quiz_name": self.quiz_name})
        self.assertIn("0 / 1 completed", setup.text)
        history = self.client.get("/quiz_attempts", query_string={"quiz_name": self.quiz_name})
        resumed = self.resume_from_page(history, missed_id)
        self.assertEqual(resumed.status_code, 302)
        self.assertIn(first["question_stem"], self.client.get(resumed.location).text)
        self.answer(missed_id, mode="study_missed", question=first["id"])
        self.assertNotIn("Resume Quiz", self.client.get(selection_path).text)
        self.assertNotIn("Resume Quiz", self.client.get(
            "/quiz_mode", query_string={"quiz_name": self.quiz_name},
        ).text)
        history = self.client.get("/quiz_attempts", query_string={"quiz_name": self.quiz_name})
        self.assertNotIn("Resume Attempt", history.text)
        self.assertIn(f"/review_attempt?attempt_id={missed_id}", history.text)
        with self.app.test_request_context():
            resume_url = url_for("quiz.resume_quiz", quiz_name=self.quiz_name)
        self.assertIn("/quiz_mode?", self.client.get(resume_url).location)

    def test_generated_quiz_resume_remains_unchanged(self):
        sample = STANDARDIZED_QUIZZES["Emergency Medicine"][0]
        questions = [dict(sample, id=f"generated-{i}", question_stem=f"Generated question {i}")
                     for i in (1, 2)]
        store_standardized_questions(self.db, questions)
        self.quiz_name = "Generated quiz"
        self.db.connection.execute(
            "INSERT INTO user_quizzes VALUES ('generated', 'student', ?, CURRENT_TIMESTAMP)",
            (self.quiz_name,),
        )
        self.db.connection.executemany(
            "INSERT INTO quiz_questions_map VALUES (?, 'generated', ?)",
            [(i, question["id"]) for i, question in enumerate(questions, 1)],
        )
        self.db.commit()
        started = self.start("study_exp")
        attempt_id = parse_qs(urlparse(started.location).query)["attempt_id"][0]
        self.answer(attempt_id, mode="study_exp", question=questions[0]["id"])
        resumed = self.client.get("/resume_quiz/Generated%20quiz")
        self.assertEqual(parse_qs(urlparse(resumed.location).query)["attempt_id"], [attempt_id])
        self.assertIn("Generated question 2", self.client.get(resumed.location).text)
        history = self.client.get("/quiz_attempts", query_string={"quiz_name": self.quiz_name})
        self.assertEqual(history.status_code, 200)
        self.assertIn("Review Attempt", history.text)
        self.assertNotIn('name="standardized_csrf_token"', history.text)

    def test_empty_and_unknown_category_do_not_create_attempts(self):
        with patch.dict(STANDARDIZED_QUIZZES, {"Emergency Medicine": []}):
            response = self.client.get("/quiz_mode", query_string={"quiz_name": self.quiz_name})
            self.assertIn("/select_standardized_quiz", response.location)
            with self.client.session_transaction() as session:
                self.assertIn("No questions", session["_flashes"][-1][1])
            self.assertEqual(self.client.post("/start_quiz", data={
                "quiz_name": self.quiz_name, "mode": "test",
            }).status_code, 302)
        self.assertEqual(self.client.get("/quiz_mode", query_string={
            "quiz_name": STANDARDIZED_PREFIX + "Unknown",
        }).status_code, 404)
        self.assertEqual(self.db.connection.execute(
            "SELECT COUNT(*) AS n FROM quiz_attempts"
        ).fetchone()["n"], 0)

    def test_empty_category_keeps_history_results_and_review_available(self):
        response = self.start()
        attempt_id = parse_qs(urlparse(response.location).query)["attempt_id"][0]
        results = self.answer(attempt_id)
        with patch.dict(STANDARDIZED_QUIZZES, {"Emergency Medicine": []}):
            self.assertEqual(self.client.get(results.location).status_code, 200)
            self.assertEqual(self.client.get("/quiz_attempts", query_string={
                "quiz_name": self.quiz_name,
            }).status_code, 200)
            self.assertEqual(self.client.get("/review_attempt", query_string={
                "attempt_id": attempt_id,
            }).status_code, 200)

    def test_regular_quiz_generation_excludes_standardized_questions(self):
        self.start()
        ordinary = dict(STANDARDIZED_QUIZZES["Emergency Medicine"][0], id="ordinary-question")
        store_standardized_questions(self.db, [ordinary])
        with patch.object(flask_app, "is_quiz_name_unique", return_value=True):
            response = self.client.post("/build_quiz_batch", json={
                "quiz_name": "Ordinary quiz", "use_existing": True,
                "blueprint": [{"organ_system": "any", "task_area": "any", "topic_area": "any"}],
            })
        self.assertEqual(response.get_json()["duplicated"], 1)
        self.assertEqual(self.db.connection.execute(
            "SELECT question_id FROM quiz_questions_map"
        ).fetchall(), [{"question_id": "ordinary-question"}])

    def test_access_requires_login_but_not_payment_or_student_role(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get("/select_standardized_quiz").location, "/")
        self.login()
        self.db.connection.execute("UPDATE users SET has_paid = 0")
        self.assertEqual(self.client.get("/select_standardized_quiz").status_code, 200)
        for flag in ("Y", "1", "TRUE"):
            self.db.connection.execute("UPDATE users SET has_paid = 0, is_faculty = ?", (flag,))
            self.assertEqual(self.client.get("/select_standardized_quiz").status_code, 200)
            self.assertEqual(self.client.get("/quiz_mode", query_string={
                "quiz_name": self.quiz_name,
            }).status_code, 200)
        self.login("guest_test")
        self.assertEqual(self.client.get("/select_standardized_quiz").status_code, 302)

    def test_invalid_start_and_cross_student_attempts_are_rejected(self):
        self.assertEqual(self.client.post("/start_quiz", data={
            "quiz_name": self.quiz_name, "mode": "test",
        }).status_code, 400)
        response = self.start()
        attempt_id = parse_qs(urlparse(response.location).query)["attempt_id"][0]
        self.assertEqual(self.answer(attempt_id, question="other-category-id").status_code, 404)
        self.assertEqual(self.answer(attempt_id, mode="study_exp").status_code, 400)
        self.db.connection.execute("INSERT INTO users VALUES ('other', 1, 'N', 'NONE')")
        self.login("other")
        self.assertEqual(self.client.get(response.location).status_code, 404)
        self.assertEqual(self.answer(attempt_id).status_code, 404)
        for path in ("/review_attempt", "/quiz_results"):
            self.assertEqual(self.client.get(path, query_string={
                "attempt_id": attempt_id,
            }).status_code, 404)

    def test_query_parameters_cannot_override_post_access_checks(self):
        response = self.start()
        attempt_id = parse_qs(urlparse(response.location).query)["attempt_id"][0]
        self.assertEqual(self.client.post("/start_quiz?quiz_name=ordinary", data={
            "quiz_name": self.quiz_name, "mode": "test",
        }).status_code, 400)
        self.assertEqual(self.client.post("/submit-answer?quiz_name=ordinary", data={
            "quiz_name": self.quiz_name, "attempt_id": attempt_id, "mode": "test",
            "question_id": "std-emergency-sample-1", "user_choice": "A",
        }).status_code, 400)
        self.assertEqual(self.client.get("/question_page", query_string={
            "quiz_name": "ordinary", "attempt_id": attempt_id,
        }).status_code, 400)
        self.assertEqual(self.client.get("/question_page", query_string={
            "q": "std-emergency-sample-1",
        }).status_code, 400)
        self.assertEqual(self.client.get("/question_page", query_string={
            "q": "std-emergency-sample-1", "question_id": "ordinary",
            "answered": "True", "mode": "study_exp",
        }).status_code, 400)

    def test_standardized_names_are_reserved_but_regular_names_are_unchanged(self):
        with self.app.test_request_context():
            self.assertFalse(flask_app.is_quiz_name_unique(
                self.db, "student", " standardized: Emergency Medicine ",
            ))
        self.assertIsNone(flask_app.get_standardized_questions("My own quiz"))
        self.assertEqual(self.client.get("/quiz_mode", query_string={
            "quiz_name": "My own quiz",
        }).status_code, 200)

    def test_repeated_sample_start_does_not_rewrite_persisted_questions(self):
        self.start()
        with patch.object(self.db, "executemany", wraps=self.db.executemany) as write:
            self.start()
        write.assert_not_called()

    def test_standardized_template_flag_does_not_fetch_question_content(self):
        with self.app.test_request_context("/quiz_mode?quiz_name=Standardized:+Unknown"), \
                patch.object(flask_app, "get_db") as db:
            self.assertTrue(flask_app.standardized_quiz_context()["is_standardized"])
        db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
