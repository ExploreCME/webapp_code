import asyncio
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
import io
import json
from pathlib import Path
import random
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import generate_standardized_quizzes as generator
from standardized_quizzes import STANDARDIZED_PREFIX, STANDARDIZED_QUIZZES
from standardizers import TASK_AREAS
import test_standardized_quizzes as quiz_tests


TOPICS = {
    "Cardiovascular System": ["Hypertension", "Heart failure", "Arrhythmia"],
    "Pulmonary System": ["Asthma", "Pneumonia"],
}
ORGANS = {"Cardiovascular System": 60, "Pulmonary System": 40}
TASKS = dict(zip(TASK_AREAS, (15, 15, 20, 10, 20, 15, 5)))


def question(item, stem):
    return {
        **item, "question_stem": stem,
        **{f"answer_choice_{letter}": f"Choice {letter}" for letter in "ABCDE"},
        "correct_answer": "B", "explanation": "Verified explanation",
        "difficulty_rating": 5,
    }


def make_versions():
    blueprints = generator.build_blueprints(TOPICS, ORGANS, TASKS, random.Random(42))
    return [[question(item, f"Question {version}-{index}")
             for index, item in enumerate(blueprint)]
            for version, blueprint in enumerate(blueprints)]


class GeneratorTests(unittest.TestCase):
    def test_exact_percentage_marginals_and_random_topic_pairings(self):
        versions = generator.build_blueprints(TOPICS, ORGANS, TASKS, random.Random(19))
        for blueprint in versions:
            self.assertEqual(len(blueprint), 120)
            self.assertEqual(Counter(item["organ_system"] for item in blueprint), {
                "Cardiovascular System": 72, "Pulmonary System": 48,
            })
            self.assertEqual(Counter(item["task_area"] for item in blueprint), {
                label: count for label, count in zip(TASK_AREAS, (18, 18, 24, 12, 24, 18, 6))
            })
            for item in blueprint:
                self.assertIn(item["topic_area"], TOPICS[item["organ_system"]])
            self.assertGreater(len({item["topic_area"] for item in blueprint}), 2)
        self.assertNotEqual(versions[0], versions[1])
        self.assertEqual(versions, generator.build_blueprints(TOPICS, ORGANS, TASKS, random.Random(19)))

    def test_rounding_and_zero_percentages(self):
        self.assertEqual(generator.allocate({
            "a": Decimal("33.33"), "b": Decimal("33.33"), "c": Decimal("33.34"),
        }), {"a": 40, "b": 40, "c": 40})
        tasks = dict.fromkeys(TASK_AREAS, 0)
        tasks[TASK_AREAS[0]] = 100
        versions = generator.build_blueprints(
            TOPICS, {"Cardiovascular System": 100, "Pulmonary System": 0}, tasks, random.Random(1),
        )
        self.assertTrue(all(item["organ_system"] == "Cardiovascular System"
                            and item["task_area"] == TASK_AREAS[0]
                            for blueprint in versions for item in blueprint))

    def test_invalid_percentages_names_and_json_are_rejected(self):
        for values in ({"a": -1, "b": 101}, {"a": "NaN", "b": 100},
                       {"a": "Infinity", "b": 0}, {"a": "oops", "b": 100},
                       {"a": 50, "b": 49}, {"a": 100}, {"a": 0, "b": 0}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                generator.validate_percentages(["a", "b"], values)
        for name in ("", " " * 5, "a" * 250, "Standardized: Emergency Medicine"):
            with self.assertRaises(ValueError):
                generator.validate_name(name)
        self.assertEqual(generator.validate_name(" emergency medicine "), "Emergency Medicine")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            for content in ('[]', '{"Cardiology": ["HTN"]}', '{"Pulmonary System": []}',
                            '{"Pulmonary System": ["Asthma"], "Pulmonary System": ["Pneumonia"]}'):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    generator.load_topics(path)

    def test_prompt_retries_invalid_totals(self):
        with patch("builtins.input", side_effect=["10", "10", "25.5", "74.5"]), redirect_stdout(io.StringIO()):
            result = generator.prompt_percentages(["a", "b"], "Percentages")
        self.assertEqual(result, {"a": Decimal("25.5"), "b": Decimal("74.5")})

    def test_cli_prompts_and_dry_run_do_not_touch_api_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            path.write_text(json.dumps(TOPICS))
            answers = ["emergency medicine", str(path), "60", "40", *map(str, TASKS.values())]
            output = io.StringIO()
            with patch("builtins.input", side_effect=answers), \
                    patch.object(generator.importlib, "import_module") as engine_import, \
                    redirect_stdout(output):
                self.assertEqual(generator.main(["--dry-run", "--seed", "3"]), 0)
            engine_import.assert_not_called()
            self.assertIn("Dry run: no API calls", output.getvalue())
            for version in range(1, 4):
                self.assertIn(f"Emergency Medicine version {version}: 120 questions", output.getvalue())

    def test_shortfall_exit_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            path.write_text(json.dumps(TOPICS))
            output = io.StringIO()
            with patch("builtins.input", side_effect=["60", "40", *map(str, TASKS.values()), "y"]), \
                    patch.dict("os.environ", {
                        "OPENAI_API_KEY": "test-only", "MYSQL_HOST": "test", "MYSQL_USER": "test",
                        "MYSQL_PASSWORD": "test-only", "MYSQL_DB": "test",
                    }), \
                    patch.object(generator, "VersionPublisher") as publisher, \
                    patch.object(generator.importlib, "import_module"), \
                    patch.object(generator, "generate_versions", new=AsyncMock(
                        side_effect=ValueError("Generation shortfall"))), \
                    redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(generator.main([
                    "--quiz-name", "Emergency Medicine", "--topics-file", str(path),
                ]), 1)
            publisher.return_value.publish.assert_not_called()

    def test_preflight_detects_existing_versions_and_closes_connection(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            {"ENGINE": "InnoDB"}, {"ENGINE": "InnoDB"},
            {"quiz_name": "Emergency Medicine version 1"},
        ]
        publisher = generator.VersionPublisher(lambda: conn, "Emergency Medicine")
        with self.assertRaisesRegex(ValueError, "already exist"):
            publisher.preflight()
        conn.close.assert_called_once()


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, responder):
        managers = []
        def manager(**kwargs):
            instance = SimpleNamespace(client=SimpleNamespace(close=AsyncMock()))
            managers.append(instance)
            return instance
        return SimpleNamespace(
            ConversationManager=manager, process_topic_batch=responder,
            standardize_categories=lambda organ, task: (organ, task), managers=managers,
        )

    async def test_shared_engine_validation_unique_stems_and_refills(self):
        sequence = 0
        active, max_active = 0, 0
        calls = Counter()
        async def responder(item_dict, num_needed, **kwargs):
            nonlocal sequence, active, max_active
            self.assertLessEqual(num_needed, generator.BATCH_SIZE)
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
            key = tuple(item_dict.values())
            calls[key] += 1
            if calls[key] == 1:
                return []  # Refill the exact same slots; never relax categories.
            results = []
            for _ in range(num_needed):
                sequence += 1
                results.append(question(item_dict, f"Verified stem {sequence}"))
            return results
        engine = self.engine(responder)
        blueprints = generator.build_blueprints(TOPICS, ORGANS, TASKS, random.Random(3))
        versions = await generator.generate_versions(blueprints, engine, "test-only", "model", None, 3, 2)
        self.assertEqual([len(version) for version in versions], [120, 120, 120])
        self.assertLessEqual(max_active, 3)
        self.assertEqual(len({q["question_stem"] for version in versions for q in version}), 360)
        for blueprint, version in zip(blueprints, versions):
            for item, result in zip(blueprint, version):
                self.assertEqual({key: result[key] for key in item}, item)
        for manager in engine.managers:
            manager.client.close.assert_awaited_once()

    async def test_single_topic_supports_all_360_slots_in_small_batches(self):
        sequence = 0
        async def responder(item_dict, num_needed, **kwargs):
            nonlocal sequence
            self.assertLessEqual(num_needed, 5)
            result = []
            for _ in range(num_needed):
                sequence += 1
                result.append(question(item_dict, f"Unique question {sequence}"))
            return result
        engine = self.engine(responder)
        tasks = dict.fromkeys(TASK_AREAS, 0)
        tasks[TASK_AREAS[0]] = 100
        blueprints = generator.build_blueprints(
            {"Cardiovascular System": ["HTN"]}, {"Cardiovascular System": 100}, tasks, random.Random(1),
        )
        versions = await generator.generate_versions(blueprints, engine, "test-only", "model", None, 5, 2)
        self.assertEqual([len(version) for version in versions], [120, 120, 120])
        self.assertEqual(sequence, 360)

    async def test_duplicate_or_misclassified_output_causes_bounded_shortfall(self):
        for kind in ("duplicate", "wrong-task"):
            calls = 0
            async def responder(item_dict, num_needed, **kwargs):
                nonlocal calls
                calls += 1
                result = question(item_dict, "Repeated stem")
                if kind == "wrong-task":
                    result["task_area"] = "Invalid task"
                return [result] * num_needed
            engine = self.engine(responder)
            tasks = dict.fromkeys(TASK_AREAS, 0)
            tasks[TASK_AREAS[0]] = 100
            blueprints = generator.build_blueprints(
                {"Cardiovascular System": ["HTN"]}, {"Cardiovascular System": 100}, tasks, random.Random(1),
            )
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "shortfall"):
                await generator.generate_versions(blueprints, engine, "test-only", "model", None, 2, 2)
            self.assertEqual(calls, 2)
            for manager in engine.managers:
                manager.client.close.assert_awaited_once()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.db = quiz_tests.QuizDatabase()
        self.addCleanup(self.db.connection.close)
        self.publisher = generator.VersionPublisher(lambda: self.db, "Emergency Medicine")

    def test_publishes_three_complete_ordered_versions_without_user_quizzes(self):
        versions = make_versions()
        self.publisher.publish(versions)
        rows = self.db.connection.execute(
            "SELECT * FROM standardized_quiz_versions ORDER BY quiz_name"
        ).fetchall()
        self.assertEqual([row["quiz_name"] for row in rows], [
            "Emergency Medicine version 1", "Emergency Medicine version 2", "Emergency Medicine version 3",
        ])
        ids = [question_id for row in rows for question_id in json.loads(row["question_ids"])]
        self.assertEqual(len(set(ids)), 360)
        self.assertTrue(all(question_id.startswith("std-") for question_id in ids))
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) AS n FROM question_bank").fetchone()["n"], 360)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) AS n FROM user_quizzes").fetchone()["n"], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.publisher.publish(make_versions())
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) AS n FROM question_bank").fetchone()["n"], 360)

    def test_incomplete_or_duplicate_sets_never_open_publication_transaction(self):
        for versions in (make_versions()[:2], [[], [], []]):
            with self.assertRaises(ValueError):
                self.publisher.publish(versions)
        versions = make_versions()
        versions[1][0]["question_stem"] = versions[0][0]["question_stem"]
        with self.assertRaises(ValueError):
            self.publisher.publish(versions)
        self.assertEqual(self.db.connection.execute(
            "SELECT COUNT(*) AS n FROM standardized_quiz_versions"
        ).fetchone()["n"], 0)

    def test_partial_database_failure_rolls_back_all_versions_and_questions(self):
        original = self.db.execute
        def execute(query, parameters=()):
            if "INSERT INTO standardized_quiz_versions" in query and parameters[0].endswith("version 2"):
                raise RuntimeError("database unavailable")
            return original(query, parameters)
        with patch.object(self.db, "execute", side_effect=execute):
            with self.assertRaises(RuntimeError):
                self.publisher.publish(make_versions())
        for table in ("question_bank", "standardized_quiz_versions"):
            self.assertEqual(self.db.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"], 0)


class PublishedVersionFlowTests(unittest.TestCase):
    setUp = quiz_tests.StandardizedQuizTests.setUp
    login = quiz_tests.StandardizedQuizTests.login
    token = quiz_tests.StandardizedQuizTests.token
    start = quiz_tests.StandardizedQuizTests.start
    answer = quiz_tests.StandardizedQuizTests.answer

    def test_published_versions_are_immediately_visible_and_playable_for_all_accounts(self):
        # Reuse the existing SQLite-backed flow fixture and real shared templates.
        generator.VersionPublisher(lambda: self.db, "Emergency Medicine").publish(make_versions())
        self.db.connection.execute("UPDATE users SET has_paid = 0")
        self.db.connection.execute("INSERT INTO users VALUES ('faculty', 0, 'Y', 'NONE')")
        self.db.connection.commit()
        self.quiz_name = STANDARDIZED_PREFIX + "Emergency Medicine version 1"
        for username in ("student", "faculty"):
            with self.subTest(username=username):
                self.login(username)
                category = self.client.get("/standardized_quizzes/Emergency%20Medicine")
                self.assertEqual(category.status_code, 200)
                html = category.get_data(as_text=True)
                self.assertEqual(html.count('class="quiz-card"'), 3)
                for version in range(1, 4):
                    self.assertIn(f"Emergency Medicine version {version}", html)
                response = self.start()
                attempt_id = parse_qs(urlparse(response.location).query)["attempt_id"][0]
                page = self.client.get(response.location).get_data(as_text=True)
                self.assertIn("Question <strong>1</strong> of <strong>120</strong>", page)
                row = self.db.connection.execute(
                    "SELECT question_ids FROM standardized_quiz_versions WHERE quiz_name = ?",
                    ("Emergency Medicine version 1",),
                ).fetchone()
                ids = json.loads(row["question_ids"])
                self.assertIn(f'value="{ids[0]}"', page)
                response = self.answer(attempt_id, choice="B", question=ids[0])
                self.assertIn("/quiz_results", response.location)
                self.assertIn("100.0%", self.client.get(response.location).get_data(as_text=True))
                review = self.client.get("/review_attempt", query_string={"attempt_id": attempt_id})
                self.assertIn("Verified explanation", review.get_data(as_text=True))
        self.login("student")
        self.assertEqual(self.client.get("/review_attempt", query_string={"attempt_id": attempt_id}).status_code, 404)

    def test_unpublished_and_guest_category_access(self):
        self.assertEqual(self.client.get("/standardized_quizzes/Unknown").status_code, 404)
        self.assertEqual(self.client.get("/quiz_mode", query_string={
            "quiz_name": STANDARDIZED_PREFIX + "Emergency Medicine version 1",
        }).status_code, 404)
        self.login("guest_test")
        self.assertEqual(self.client.get("/standardized_quizzes/Emergency%20Medicine").status_code, 302)


if __name__ == "__main__":
    unittest.main()
