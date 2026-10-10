import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import quote

from flask import Flask

# Importing the blueprint should not create its production upload directory.
with patch("os.makedirs"):
    import flask_app


class ProgressDatabase:
    """Exercise generated normalized keys locally, not MySQL collation/DDL."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = lambda cursor, row: dict(
            zip((column[0] for column in cursor.description), row)
        )
        self.queries = []
        self.connection.executescript("""
            CREATE TABLE flashcards (
                id INTEGER PRIMARY KEY, organ_system TEXT, topic TEXT,
                task_area TEXT, prompt TEXT, answer TEXT,
                organ_system_norm TEXT GENERATED ALWAYS AS
                    (LOWER(TRIM(organ_system))) STORED,
                topic_norm TEXT GENERATED ALWAYS AS
                    (LOWER(TRIM(topic))) STORED
            );
            CREATE TABLE flashcard_progress (
                username TEXT NOT NULL, organ_system TEXT NOT NULL,
                topic TEXT NOT NULL, current_index INTEGER,
                organ_system_norm TEXT GENERATED ALWAYS AS
                    (LOWER(TRIM(organ_system))) STORED,
                topic_norm TEXT GENERATED ALWAYS AS
                    (LOWER(TRIM(topic))) STORED,
                PRIMARY KEY (username, organ_system, topic)
            );
            CREATE UNIQUE INDEX idx_flashcard_progress_norm_unique
                ON flashcard_progress (username, organ_system_norm, topic_norm);
        """)
        self.connection.executemany(
            "INSERT INTO flashcards (id, organ_system, topic, prompt, answer) "
            "VALUES (?, ?, ?, 'Prompt', 'Answer')",
            [
                (1, "Cardiology", "Hypertension"),
                (2, "Cardiology", "Coronary Disease"),
                (3, " CARDIOLOGY ", " hypertension "),
                (4, "Cardiology", None),
                (5, "Cardiology", ""),
                (6, "Cardiology", "  "),
                (7, "Renal", "Hypertension"),
                (8, "Cardiology", "Hypertension"),
            ],
        )
        self.connection.commit()

    def cursor(self):
        return self

    def execute(self, query, parameters):
        self.queries.append((" ".join(query.split()), parameters))
        # Translate only DB-API placeholders and MySQL's upsert syntax.
        query = query.replace("%s", "?").replace(
            "ON DUPLICATE KEY UPDATE current_index = VALUES(current_index)",
            "ON CONFLICT (username, organ_system_norm, topic_norm) "
            "DO UPDATE SET current_index = excluded.current_index",
        )
        self.result = self.connection.execute(query, parameters)

    def fetchall(self):
        return self.result.fetchall()

    def fetchone(self):
        return self.result.fetchone()

    def commit(self):
        self.connection.commit()


class FlashcardTests(unittest.TestCase):
    def setUp(self):
        self.db = ProgressDatabase()
        self.addCleanup(self.db.connection.close)
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="flashcard-test-only")
        app.register_blueprint(flask_app.quiz_bp)
        self.client = app.test_client()
        self.login(" User_X ")
        db_patch = patch.object(flask_app, "get_db", return_value=self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        render_patch = patch.object(flask_app, "render_template", return_value="OK")
        self.render = render_patch.start()
        self.addCleanup(render_patch.stop)

    def login(self, username):
        with self.client.session_transaction() as session:
            session["username"] = username
            session["accepted_eula"] = True

    def save(self, organ="Cardiology", topic="Hypertension", index=1):
        return self.client.post("/save_flashcard_progress", json={
            "organ_system": organ, "topic": topic, "current_index": index,
        })

    def viewer(self, organ=" cardiology ", topic=" HYPERTENSION "):
        response = self.client.get(
            f"/flashcards/{quote(organ, safe='')}/{quote(topic, safe='')}"
        )
        self.assertEqual(response.status_code, 200)
        return self.render.call_args.kwargs

    def test_topic_selection_uses_normalized_columns_and_raw_ordered_labels(self):
        response = self.client.get("/flashcards/%20CARDIOLOGY%20")
        self.assertEqual(response.status_code, 200)
        context = self.render.call_args.kwargs
        self.assertEqual(context["organ_system"], " CARDIOLOGY ")
        self.assertEqual(context["topics"], [
            " hypertension ", "Coronary Disease", "Hypertension",
        ])
        query, parameters = self.db.queries[-1]
        self.assertIn("SELECT DISTINCT topic", query)
        self.assertIn("organ_system_norm = LOWER(TRIM(%s))", query)
        self.assertIn("topic_norm IS NOT NULL AND topic_norm != ''", query)
        self.assertIn("ORDER BY topic ASC", query)
        self.assertEqual(parameters, (" CARDIOLOGY ",))

    def test_viewer_uses_normalized_columns_preserving_labels_and_card_order(self):
        context = self.viewer()
        self.assertEqual([card["id"] for card in context["cards"]], [1, 3, 8])
        self.assertEqual(context["cards"][1]["topic"], " hypertension ")
        self.assertEqual(context["organ_system"], " cardiology ")
        self.assertEqual(context["topic"], " HYPERTENSION ")
        self.assertEqual(context["saved_index"], 0)
        query, parameters = self.db.queries[-2]
        self.assertIn("organ_system_norm = LOWER(TRIM(%s))", query)
        self.assertIn("topic_norm = LOWER(TRIM(%s))", query)
        self.assertIn("ORDER BY id ASC", query)
        self.assertEqual(parameters, (" cardiology ", " HYPERTENSION "))
        query, parameters = self.db.queries[-1]
        self.assertIn(
            "username = %s AND organ_system_norm = LOWER(TRIM(%s)) "
            "AND topic_norm = LOWER(TRIM(%s))", query,
        )
        self.assertEqual(parameters, (" User_X ", " cardiology ", " HYPERTENSION "))

    def test_save_variants_upsert_one_raw_row_and_resume_latest_index(self):
        self.assertEqual(self.save().get_json(), {"status": "success"})
        response = self.save(" CARDIOLOGY ", " hypertension ", 2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "success"})
        self.assertEqual(self.viewer()["saved_index"], 2)
        rows = self.db.connection.execute(
            "SELECT username, organ_system, topic, current_index FROM flashcard_progress"
        ).fetchall()
        self.assertEqual(rows, [{
            "username": " User_X ", "organ_system": "Cardiology",
            "topic": "Hypertension", "current_index": 2,
        }])
        writes = [entry for entry in self.db.queries if entry[0].startswith("INSERT")]
        self.assertEqual(writes[-1][1], (" User_X ", " CARDIOLOGY ", " hypertension ", 2))
        self.assertIn(
            "INSERT INTO flashcard_progress (username, organ_system, topic, current_index)",
            writes[-1][0],
        )

    def test_usernames_are_isolated_without_case_or_space_normalization(self):
        self.save(index=2)
        for username in ("user_x", "User_X", "other_user"):
            with self.subTest(username=username):
                self.login(username)
                self.assertEqual(self.viewer()["saved_index"], 0)
                self.save(index=1)
                self.assertEqual(self.viewer()["saved_index"], 1)
        self.login(" User_X ")
        self.assertEqual(self.viewer()["saved_index"], 2)
        rows = self.db.connection.execute("SELECT username FROM flashcard_progress").fetchall()
        self.assertEqual(len(rows), 4)

    def test_all_collection_keeps_all_topics_and_separate_progress(self):
        self.save(index=2)
        response = self.client.post("/save_flashcard_progress", json={
            "organ_system": "Cardiology", "current_index": 5,
        })
        self.assertEqual(response.get_json(), {"status": "success"})
        for topic in ("All", "ALL", "aLl"):
            with self.subTest(topic=topic):
                context = self.viewer(topic=topic)
                self.assertEqual([card["id"] for card in context["cards"]], [1, 2, 3, 4, 5, 6, 8])
                self.assertEqual(context["saved_index"], 5)
                self.assertEqual(self.db.queries[-2][1], (" cardiology ",))
                self.assertNotIn("topic_norm =", self.db.queries[-2][0])
        self.assertEqual(self.viewer()["saved_index"], 2)

    def test_index_zero_and_existing_viewer_bounds(self):
        self.assertEqual(self.save(index=0).status_code, 200)
        self.assertEqual(self.viewer()["saved_index"], 0)
        self.save(index=99)
        self.assertEqual(self.viewer()["saved_index"], 2)

    def test_invalid_json_is_rejected_before_database_access(self):
        for body in ("", "{", "null", "[]", '"text"', "42", "true"):
            with self.subTest(body=body):
                response = self.client.post(
                    "/save_flashcard_progress", data=body, content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json(), {"status": "error"})
        response = self.client.post("/save_flashcard_progress", data="not JSON")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.queries, [])

    def test_missing_fields_and_invalid_indexes_are_rejected(self):
        payloads = [{}, {"organ_system": "Cardiology"}, {"current_index": 0}]
        payloads += [
            {"organ_system": "Cardiology", "current_index": index}
            for index in (None, -1, 1.5, 1.0, True, False, "2", "bad", [], {})
        ]
        payloads += [
            {"organ_system": organ, "current_index": 0}
            for organ in (None, "", "  ", 42, [], {})
        ]
        payloads += [
            {"organ_system": "Cardiology", "current_index": 0, "topic": topic}
            for topic in (None, 42, [], {})
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/save_flashcard_progress", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json(), {"status": "error"})
        self.assertEqual(self.db.queries, [])

    def test_unauthenticated_save_still_returns_401(self):
        with self.client.session_transaction() as session:
            session.pop("username")
        self.assertEqual(self.save().status_code, 401)
        self.assertEqual(self.db.queries, [])


if __name__ == "__main__":
    unittest.main()
