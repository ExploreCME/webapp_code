import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from flask import Flask, render_template
from jinja2 import ChoiceLoader, DictLoader

import flask_app
from standardized_quizzes import STANDARDIZED_PREFIX, STANDARDIZED_QUIZZES, store_standardized_questions


class QuizDatabase:
    """Exercise shared quiz SQL locally, translating only MySQL-specific syntax."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
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
                due_date TEXT, required_mode TEXT, assigned_date TEXT
            );
            CREATE TABLE user_quizzes (quiz_id TEXT, username TEXT, quiz_name TEXT, created_date TEXT);
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
        self.result = self.connection.execute(self.translate(query), parameters)

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
        self.app.add_url_rule("/", "paxcel_landing", lambda: "Sign in")
        self.app.add_url_rule("/statistics", "stats.my_statistics", lambda: "Statistics")
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": (
                "{% block head %}{% endblock %}"
                "{% for message in get_flashed_messages() %}{{ message }}{% endfor %}"
                "{% block content %}{% endblock %}"
            )}),
            self.app.jinja_loader,
        ])
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

    def test_dashboard_link_and_six_category_tiles(self):
        with self.app.test_request_context():
            dashboard = render_template("student_dashboard.html")
        self.assertIn('href="/select_standardized_quiz"', dashboard)
        page = self.client.get("/select_standardized_quiz").get_data(as_text=True)
        self.assertEqual(page.count('class="quiz-card"'), 6)
        self.assertEqual(list(STANDARDIZED_QUIZZES), [
            "Emergency Medicine", "Family Medicine", "Internal Medicine",
            "Pediatrics", "Behavioral Health", "Women's Health",
        ])
        for category in STANDARDIZED_QUIZZES:
            self.assertIn(category.replace("'", "&#39;"), page)
        self.assertIn(".quiz-grid", page)
        self.assertEqual(page.count("View Attempts"), 6)

    def test_every_category_uses_shared_setup_take_scoring_and_review(self):
        for category, questions in STANDARDIZED_QUIZZES.items():
            with self.subTest(category=category):
                self.quiz_name = STANDARDIZED_PREFIX + category
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
        self.assertIn("all 1 available question(s)", html)
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

    def test_access_requires_premium_student_login(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get("/select_standardized_quiz").location, "/")
        self.login()
        self.db.connection.execute("UPDATE users SET has_paid = 0")
        self.assertEqual(self.client.get("/select_standardized_quiz").status_code, 302)
        for flag in ("Y", "1", "TRUE"):
            self.db.connection.execute("UPDATE users SET has_paid = 1, is_faculty = ?", (flag,))
            self.assertEqual(self.client.get("/select_standardized_quiz").status_code, 403)
            self.assertEqual(self.client.get("/quiz_mode", query_string={
                "quiz_name": self.quiz_name,
            }).status_code, 403)

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
        self.db.connection.execute("UPDATE users SET is_faculty = 'Y'")
        self.assertEqual(self.client.post("/start_quiz?quiz_name=ordinary", data={
            "quiz_name": self.quiz_name, "mode": "test",
            "standardized_csrf_token": self.token(),
        }).status_code, 403)
        self.assertEqual(self.client.post("/submit-answer?quiz_name=ordinary", data={
            "quiz_name": self.quiz_name, "attempt_id": attempt_id, "mode": "test",
            "question_id": "std-emergency-sample-1", "user_choice": "A",
            "standardized_csrf_token": self.token(),
        }).status_code, 403)
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


if __name__ == "__main__":
    unittest.main()
