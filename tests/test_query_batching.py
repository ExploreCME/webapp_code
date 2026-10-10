import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch
from urllib.parse import quote

from flask import Flask

# Importing the blueprint should not create its production upload directory.
with patch("os.makedirs"):
    import flask_app


class QuizDatabase:
    """Local SQLite stand-in that records every query sent through the cursor."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = lambda cursor, row: dict(
            zip((column[0] for column in cursor.description), row)
        )
        self.connection.create_function(
            "NOW", 0, lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        self.queries = []
        self.connection.executescript("""
            CREATE TABLE users (
                clerk_id TEXT, has_paid INTEGER, is_faculty TEXT,
                program_code TEXT, accepted_eula INTEGER
            );
            CREATE TABLE class_members (class_id INTEGER, clerk_id TEXT);
            CREATE TABLE assigned_quizzes (
                quiz_name TEXT, clerk_id TEXT, class_id INTEGER, program_code TEXT,
                assigned_by TEXT, due_date TEXT, required_mode TEXT, assigned_date TEXT
            );
            CREATE TABLE assigned_remediations (
                remediation_name TEXT, clerk_id TEXT, class_id INTEGER, program_code TEXT,
                assigned_by TEXT, due_date TEXT, required_mode TEXT, assigned_date TEXT
            );
            CREATE TABLE user_quizzes (
                quiz_id INTEGER PRIMARY KEY, username TEXT, quiz_name TEXT, created_date TEXT
            );
            CREATE TABLE question_bank (
                id INTEGER PRIMARY KEY, question_stem TEXT, correct_answer TEXT
            );
            CREATE TABLE quiz_questions_map (
                map_id INTEGER PRIMARY KEY, quiz_id INTEGER, question_id INTEGER
            );
            CREATE TABLE quiz_attempts (
                attempt_id TEXT PRIMARY KEY, username TEXT, quiz_name TEXT,
                mode TEXT, start_time TEXT
            );
            CREATE TABLE attempt_responses (
                id INTEGER PRIMARY KEY, attempt_id TEXT, question_id TEXT,
                user_choice TEXT, correct_answer TEXT, is_correct INTEGER,
                submitted_time TEXT, UNIQUE (attempt_id, question_id)
            );
            CREATE TABLE remediation_assignments (
                id INTEGER PRIMARY KEY, username TEXT, remediation_name TEXT,
                topic_area TEXT, task_area TEXT, created_date TEXT
            );
            CREATE TABLE remediation_questions (
                id INTEGER PRIMARY KEY, assignment_id INTEGER, question_stem TEXT
            );
        """)

    def cursor(self):
        return self

    def execute(self, query, parameters=()):
        self.queries.append((" ".join(query.split()), tuple(parameters)))
        self.result = self.connection.execute(query.replace("%s", "?"), tuple(parameters))

    def fetchall(self):
        return self.result.fetchall()

    def fetchone(self):
        return self.result.fetchone()

    def commit(self):
        self.connection.commit()

    def seed(self, sql, rows):
        self.connection.executemany(sql, rows)
        self.connection.commit()


class QueryBatchingTests(unittest.TestCase):
    def setUp(self):
        self.db = QuizDatabase()
        self.addCleanup(self.db.connection.close)
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="query-batching-test-only")
        app.register_blueprint(flask_app.quiz_bp)
        app.add_url_rule("/", "index", lambda: "index")
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "student_1"
            session["accepted_eula"] = True
        db_patch = patch.object(flask_app, "get_db", return_value=self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        render_patch = patch.object(flask_app, "render_template", return_value="OK")
        self.render = render_patch.start()
        self.addCleanup(render_patch.stop)

    def seed_quiz(self, num_questions=4):
        self.db.seed("INSERT INTO user_quizzes (quiz_id, username, quiz_name) VALUES (?, ?, ?)",
                     [(1, "student_1", "Cardio Quiz")])
        question_ids = [100 + i for i in range(num_questions)]
        self.db.seed("INSERT INTO question_bank (id, question_stem, correct_answer) VALUES (?, 'Stem', 'A')",
                     [(q_id,) for q_id in question_ids])
        # Map order intentionally differs from id order.
        self.db.seed("INSERT INTO quiz_questions_map (map_id, quiz_id, question_id) VALUES (?, 1, ?)",
                     [(i + 1, q_id) for i, q_id in enumerate(reversed(question_ids))])
        return list(reversed(question_ids))

    def add_attempt(self, attempt_id, start_time, answered_ids, quiz_name="Cardio Quiz", mode="study"):
        self.db.seed("INSERT INTO quiz_attempts VALUES (?, 'student_1', ?, ?, ?)",
                     [(attempt_id, quiz_name, mode, start_time)])
        self.db.seed("INSERT INTO attempt_responses (attempt_id, question_id, user_choice, is_correct) "
                     "VALUES (?, ?, 'A', ?)",
                     [(attempt_id, str(q_id), i % 2) for i, q_id in enumerate(answered_ids)])

    def context(self):
        return self.render.call_args.kwargs

    def test_assignment_scope_combines_user_and_class_lookup(self):
        self.db.seed("INSERT INTO users (clerk_id, has_paid, program_code) VALUES (?, ?, ?)",
                     [("student_1", 1, "PA1")])
        self.db.seed("INSERT INTO class_members VALUES (?, ?)",
                     [(7, "student_1"), (9, "student_1"), (8, "other")])
        user_row, class_ids = flask_app._get_user_assignment_scope(self.db, "student_1")
        self.assertEqual(user_row, {"has_paid": 1, "program_code": "PA1"})
        self.assertEqual(sorted(class_ids), [7, 9])
        self.assertEqual(len(self.db.queries), 1)

        # Users without a users row still keep their class memberships.
        self.db.seed("INSERT INTO class_members VALUES (?, ?)", [(3, "no_row")])
        self.assertEqual(flask_app._get_user_assignment_scope(self.db, "no_row"), (None, [3]))
        self.assertEqual(flask_app._get_user_assignment_scope(self.db, "nobody"), (None, []))

    def test_select_quiz_still_requires_premium_users_row(self):
        response = self.client.get("/select_quiz")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard", response.headers["Location"])

    def test_quiz_mode_counts_attempts_in_one_query(self):
        quiz_ids = self.seed_quiz()
        self.db.seed("INSERT INTO users (clerk_id, has_paid, program_code) VALUES (?, 1, 'PA1')",
                     [("student_1",)])
        self.db.seed("INSERT INTO class_members VALUES (?, ?)", [(5, "student_1")])
        self.add_attempt("old", "2026-01-01 08:00:00", quiz_ids[:1])
        self.add_attempt("done", "2026-01-02 08:00:00", quiz_ids)
        self.add_attempt("new", "2026-01-03 08:00:00", quiz_ids[:3], mode="test")
        self.add_attempt("empty", "2026-01-04 08:00:00", [])

        response = self.client.get("/quiz_mode?quiz_name=" + quote("Cardio Quiz"))
        self.assertEqual(response.status_code, 200)
        attempts = self.context()["incomplete_attempts"]
        self.assertEqual(
            [(a["attempt_id"], a["mode"], a["answered"], a["total"], a["start_time"]) for a in attempts],
            [("empty", "study", 0, 4, "2026-01-04"), ("new", "test", 3, 4, "2026-01-03"),
             ("old", "study", 1, 4, "2026-01-01")],
        )
        self.assertIsNone(self.context()["forced_mode"])
        per_attempt = [q for q in self.db.queries if "FROM attempt_responses WHERE attempt_id" in q[0]]
        self.assertEqual(per_attempt, [])
        # remediation check, user scope, assignment, total, grouped attempts
        self.assertEqual(len(self.db.queries), 5)
        assignment_query = self.db.queries[2]
        self.assertEqual(assignment_query[1], ("Cardio Quiz", "student_1", "PA1", "5"))

    def test_remediation_modules_aggregates_in_one_query(self):
        self.db.seed("INSERT INTO remediation_assignments VALUES (?, 'student_1', ?, ?, ?, ?)", [
            (1, "Rem A", "Topic 1", "Task 1", "2026-01-01"),
            (2, " rem a ", "Topic 2", "Task 2", "2026-01-02"),
            (3, "Rem A", "Topic 3", "Task 3", "2026-01-03"),
            (4, "Other", "Topic 4", "Task 4", "2026-01-01"),
        ])
        self.db.seed("INSERT INTO remediation_questions (id, assignment_id) VALUES (?, ?)",
                     [(11, 1), (12, 1), (13, 1), (21, 2), (22, 2), (41, 4)])
        self.db.seed("INSERT INTO attempt_responses (attempt_id, question_id, is_correct) VALUES (?, ?, ?)",
                     [("att", "11", 1), ("att", "12", 0), ("att", "21", 1), ("att", "22", 1),
                      ("other_att", "13", 1), ("att", "41", 1)])

        response = self.client.get("/remediation/modules/Rem%20A?attempt_id=att")
        self.assertEqual(response.status_code, 200)
        context = self.context()
        self.assertEqual(context["modules"], [
            {"id": 1, "topic_area": "Topic 1", "task_area": "Task 1", "total": 3,
             "answered": 2, "correct": 1, "is_complete": False},
            {"id": 2, "topic_area": "Topic 2", "task_area": "Task 2", "total": 2,
             "answered": 2, "correct": 2, "is_complete": True},
            {"id": 3, "topic_area": "Topic 3", "task_area": "Task 3", "total": 0,
             "answered": 0, "correct": 0, "is_complete": True},
        ])
        self.assertFalse(context["all_complete"])
        self.assertEqual(len(self.db.queries), 1)

    def test_question_page_resumes_first_unanswered_without_extra_query(self):
        quiz_ids = self.seed_quiz()
        self.add_attempt("att", "2026-01-01 08:00:00", [quiz_ids[0], quiz_ids[2]])

        self.client.get("/question_page?quiz_name=" + quote("Cardio Quiz") + "&attempt_id=att&mode=study")
        context = self.context()
        self.assertEqual(context["question"]["id"], quiz_ids[1])
        self.assertEqual(context["current_question_number"], 2)
        self.assertEqual([g["status"] for g in context["grid_data"]],
                         ["incorrect", "unanswered", "correct", "unanswered"])
        self.assertEqual(sum("FROM attempt_responses WHERE attempt_id" in q[0] for q in self.db.queries), 1)
        self.assertFalse(any("ar.id IS NULL" in q[0] for q in self.db.queries))

        # Fully answered attempts fall back to the first question, as before.
        self.add_attempt("full", "2026-01-02 08:00:00", quiz_ids)
        self.client.get("/question_page?quiz_name=" + quote("Cardio Quiz") + "&attempt_id=full&mode=test")
        self.assertEqual(self.context()["question"]["id"], quiz_ids[0])
        self.assertEqual(self.context()["previous_choice"], "A")

    def test_remediation_question_page_resumes_without_extra_query(self):
        self.db.seed("INSERT INTO remediation_assignments VALUES (1, 'student_1', 'Rem A', 'T', 'TA', '2026-01-01')", [()])
        self.db.seed("INSERT INTO remediation_questions (id, assignment_id, question_stem) VALUES (?, 1, 'Stem')",
                     [(31,), (32,), (33,)])
        self.add_attempt("att", "2026-01-01 08:00:00", [31], quiz_name="Rem A", mode="study")

        self.client.get("/remediation/questions/1?attempt_id=att")
        context = self.context()
        self.assertEqual(context["question"]["id"], 32)
        self.assertEqual([g["status"] for g in context["grid_data"]],
                         ["incorrect", "unanswered", "unanswered"])
        self.assertEqual(sum("FROM attempt_responses WHERE attempt_id" in q[0] for q in self.db.queries), 1)
        self.assertFalse(any("ar.id IS NULL" in q[0] for q in self.db.queries))


if __name__ == "__main__":
    unittest.main()
